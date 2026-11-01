"""레슨 풀이 API (설계 탭 'API 명세'의 레슨 영역 5개)

- 응답은 JSON, 에러는 {"error": {"code", "message"}} 한 가지 모양으로 보낸다.
- 정답(answer)은 '해설 보기'를 누른 뒤에만 보낸다. 채점은 서버에서만 한다.
- 로그인 없이는 첫 레슨만 풀 수 있다(FR-01). 게스트 기록은 세션의 guest_key로 묶는다.
"""

import hmac
import json
import logging
import math
import os
import re
import secrets
from datetime import time as dtime, timedelta

from django.core.cache import cache
from django.db import IntegrityError
from django.db.models import Count
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .embeddings import EmbeddingError
from .hints import DAILY_LIMIT, LLMError, generate_hint
from . import push, webpush
from .models import (AnswerAttempt, ExplanationReveal, HintRequest, Lesson, LessonProgress, LessonView, Profile,
                     PushDelivery, PushSubscription, Question)

logger = logging.getLogger(__name__)

RECORD_WEEKS_MAX = 12  # 주별 기록은 최대 12주까지 (화면은 4주)
HINT_SUGGEST_STREAK = 2  # 같은 문제를 연속 2번 틀리면 사수가 힌트를 제안한다 (FR-09)


# ---------- 공통 도우미 ----------

def ok(data, status=200):
    # ensure_ascii=False: 한글을 \uXXXX 대신 그대로 보낸다 (브라우저·로그에서 읽기 쉽게)
    return JsonResponse(data, status=status, json_dumps_params={"ensure_ascii": False})


def error(code, message, status):
    return ok({"error": {"code": code, "message": message}}, status=status)


def read_json(request):
    """요청 본문을 dict로 읽는다. 비어 있으면 빈 dict, 형식이 틀리면 None."""
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def is_number(value):
    """정수인지 확인. bool은 int의 한 종류라 따로 막는다 (true가 1번으로 채점되지 않게)."""
    return isinstance(value, int) and not isinstance(value, bool)


def get_guest_key(request):
    """게스트 식별값. 세션 키 대신 세션 '안에' 저장한다.
    Django는 로그인할 때 세션 키를 새로 바꾸지만 세션 데이터는 그대로 옮기므로,
    이렇게 해야 가입·로그인 뒤에도 게스트 기록을 찾아 계정에 붙일 수 있다."""
    if "guest_key" not in request.session:
        request.session["guest_key"] = secrets.token_urlsafe(16)
    return request.session["guest_key"]


def published_lessons():
    """오늘의 레슨 후보: 공개된 과정의 공개된 일반 레슨(진단 제외), 과정·순서대로."""
    return (
        Lesson.objects.filter(
            is_published=True, course__is_published=True, kind=Lesson.Kind.LESSON
        )
        .order_by("course_id", "order_no")
    )


def openable_lessons(request):
    """레슨 화면·풀이 시작에서 열 수 있는 레슨.
    관리자(is_staff)는 공개 전 초안도 실제 화면으로 미리 본다 (레슨 공개 전 폰 점검).
    오늘의 레슨(today)은 항상 공개 레슨에서만 고른다."""
    if request.user.is_authenticated and request.user.is_staff:
        return Lesson.objects.filter(kind=Lesson.Kind.LESSON)
    return published_lessons()


DIAG_ORDER_NO = {"A": 101, "B": 102}  # 레슨 파일 diag_A·diag_B의 번호 (레슨 검사기 규칙)


def diagnostic_lesson(letter):
    return Lesson.objects.filter(
        kind=Lesson.Kind.DIAGNOSTIC, order_no=DIAG_ORDER_NO[letter],
        is_published=True, course__is_published=True,
    ).first()


def pending_pre_diagnostic(user):
    """아직 풀지 않은 사전 진단 레슨. 없거나 이미 끝났거나 건너뛰었으면 None (FR-03).
    사전 진단은 배우기 전 실력을 재는 것이라, 레슨을 2개 이상 끝낸 사람에게는 내지 않는다
    (가입 전 게스트로 푼 첫 레슨 1개는 허용)"""
    profile = get_profile(user)
    if profile.onboarded_at is None or profile.pre_diag_skipped_at is not None:
        return None
    done = LessonProgress.objects.filter(user=user, completed_at__isnull=False)
    if done.filter(phase=LessonProgress.Phase.PRE).exists():
        return None
    if done.filter(lesson__kind=Lesson.Kind.LESSON).values("lesson_id").distinct().count() >= 2:
        return None
    return diagnostic_lesson(profile.diag_order[0])


def find_lesson(request, lesson_id):
    """레슨 화면·풀이 시작에서 열 수 있는 레슨. 일반 레슨 + 내 차례인 사전 진단"""
    lesson = openable_lessons(request).select_related("course").filter(id=lesson_id).first()
    if lesson is None and request.user.is_authenticated:
        diag = pending_pre_diagnostic(request.user)
        if diag is not None and diag.id == lesson_id:
            lesson = diag
    return lesson


def is_diagnostic(lesson):
    return lesson.kind == Lesson.Kind.DIAGNOSTIC


def is_preview(lesson):
    return not (lesson.is_published and lesson.course.is_published)


def first_lesson():
    return published_lessons().first()


def can_open(request, lesson):
    """로그인했으면 공개 레슨 전부, 게스트는 첫 레슨만."""
    if request.user.is_authenticated:
        return True
    first = first_lesson()
    return first is not None and lesson.id == first.id


def owns(request, progress):
    """남의 풀이 기록은 없는 것처럼 404로 처리하기 위해 주인인지 확인한다."""
    if request.user.is_authenticated:
        return progress.user_id == request.user.id
    return progress.user_id is None and progress.guest_key == request.session.get("guest_key")


def get_profile(user):
    """사용자 설정. 처음 필요할 때 기본값(주 5일)으로 만든다"""
    profile, _ = Profile.objects.get_or_create(user=user)
    return profile


def this_monday():
    today = timezone.localdate()
    return today - timedelta(days=today.weekday())


def study_dates(user, since):
    """since(날짜)부터 레슨을 끝낸 날짜들(한국 시간). 하루에 여러 개 끝내도 1일 (FR-11).
    관리자가 공개 전 초안을 미리 푼 기록과 진단은 넣지 않는다 (주간 목표 = 레슨을 푼 날, 베타 달성률이 부풀지 않게)"""
    completed = LessonProgress.objects.filter(
        user=user, completed_at__date__gte=since, lesson__kind=Lesson.Kind.LESSON,
        lesson__is_published=True, lesson__course__is_published=True,
    ).values_list("completed_at", flat=True)
    return {timezone.localtime(t).date() for t in completed}


def week_summary(user):
    """이번 주(월요일 시작, 한국 시간) 레슨을 끝낸 날 수와 내가 고른 주간 목표"""
    return {"done_days": len(study_dates(user, this_monday())), "goal_days": get_profile(user).weekly_goal_days}


def estimate_finish(remaining, goal_days):
    """예상 완료일 (FR-12): 남은 레슨 수 ÷ 주간 목표. 하루에 레슨 1개, 주 goal_days일 푼다고 보고 계산한 대략값"""
    if remaining == 0:
        return None
    days = math.ceil(remaining * 7 / goal_days)
    return {"days": days, "date": (timezone.localdate() + timedelta(days=days)).isoformat()}


def lesson_brief(lesson):
    return {
        "id": lesson.id,
        "order_no": lesson.order_no,
        "title": lesson.title,
        "track": lesson.track,
        "est_minutes": lesson.est_minutes,
    }


# ---------- API ----------

@ensure_csrf_cookie  # 화면(JS)이 POST할 때 쓸 CSRF 쿠키를 여기서 받아 간다
@require_GET
def today(request):
    """GET /api/v1/today : 오늘 풀 레슨 1개와 이번 주 진행 (FR-04, FR-11)"""
    if not request.user.is_authenticated:
        lesson = first_lesson()
        return ok({
            "lesson": lesson_brief(lesson) if lesson else None,
            "week": None,
            "needs_onboarding": False,
            "is_guest": True,
        })

    done_ids = LessonProgress.objects.filter(
        user=request.user, completed_at__isnull=False
    ).values_list("lesson_id", flat=True)
    # 아직 끝내지 않은 첫 레슨. 전부 끝냈으면 None
    lesson = published_lessons().exclude(id__in=done_ids).first()
    pre = pending_pre_diagnostic(request.user)
    return ok({
        "lesson": lesson_brief(lesson) if lesson else None,
        "week": week_summary(request.user),
        # 처음 로그인하면 홈에서 주간 목표부터 고른다 (FR-02)
        "needs_onboarding": get_profile(request.user).onboarded_at is None,
        # 목표를 고른 뒤 첫 레슨 전에 사전 진단을 권한다 (FR-03). 끝냈거나 건너뛰었으면 None
        "pre_diagnostic": lesson_brief(pre) if pre else None,
        # 알림을 켰는지 (홈에서 '매일 알림 받기' 링크를 보여 줄지). 서버 알림 설정이 없으면 None
        "notify_on": (bool(get_profile(request.user).notify_time) and request.user.push_subscriptions.exists())
        if webpush.vapid_private_key() else None,
        "is_guest": False,
    })


@require_GET
def lesson_detail(request, lesson_id):
    """GET /api/v1/lessons/{id} : 레슨 본문과 문제. 정답·해설은 보내지 않는다 (FR-05)"""
    lesson = find_lesson(request, lesson_id)
    if lesson is None:
        return error("NOT_FOUND", "레슨을 찾을 수 없어요.", 404)
    if not can_open(request, lesson):
        return error("NOT_AUTHENTICATED", "로그인하면 다음 레슨을 풀 수 있어요.", 401)

    # 단계별 이탈을 재려고 레슨을 연 기록을 남긴다 (6.1). 게스트는 가입하면 계정으로 옮긴다(signals)
    if request.user.is_authenticated:
        LessonView.objects.create(user=request.user, lesson=lesson)
    else:
        LessonView.objects.create(guest_key=get_guest_key(request), lesson=lesson)

    questions = [
        {
            "id": q.id,
            "order_no": q.order_no,
            "qtype": q.qtype,
            "prompt": q.prompt,
            "code": q.code,
            "choices": q.choices,
        }
        for q in lesson.questions.order_by("order_no")
    ]
    return ok({
        **lesson_brief(lesson),
        "body_md": lesson.body_md,
        "questions": questions,
        "is_preview": is_preview(lesson),  # 관리자가 공개 전 초안을 보는 중
        "is_diagnostic": is_diagnostic(lesson),  # 진단: 정답 표시·해설·힌트 없이 한 번씩만 답한다
    })


@require_POST
def start_progress(request, lesson_id):
    """POST /api/v1/lessons/{id}/progress : 풀이 시작. 다시 풀면 새 기록 (FR-07)"""
    lesson = find_lesson(request, lesson_id)
    if lesson is None:
        return error("NOT_FOUND", "레슨을 찾을 수 없어요.", 404)
    if not can_open(request, lesson):
        return error("NOT_AUTHENTICATED", "로그인하면 다음 레슨을 풀 수 있어요.", 401)

    data = read_json(request)
    if data is None:
        return error("VALIDATION_ERROR", "요청 형식이 올바르지 않아요.", 400)
    # 사전·사후 구분은 서버가 정한다. 지금 열 수 있는 진단은 사전 진단뿐이다 (사후는 베타 마지막 주에 연다)
    phase = LessonProgress.Phase.PRE if is_diagnostic(lesson) else ""

    if request.user.is_authenticated:
        progress = LessonProgress.objects.create(user=request.user, lesson=lesson, phase=phase)
    else:
        progress = LessonProgress.objects.create(
            guest_key=get_guest_key(request), lesson=lesson, phase=phase
        )
    return ok(
        {"progress_id": progress.id, "started_at": progress.started_at.isoformat()},
        status=201,
    )


@require_POST
def submit_answer(request, progress_id):
    """POST /api/v1/progress/{id}/answers : 채점하고 해설을 돌려준다 (FR-06, FR-09)"""
    progress = LessonProgress.objects.filter(id=progress_id).first()
    if progress is None or not owns(request, progress):
        return error("NOT_FOUND", "풀이 기록을 찾을 수 없어요.", 404)
    if progress.completed_at is not None:
        return error("VALIDATION_ERROR", "이미 끝낸 풀이예요. 다시 풀려면 새로 시작해 주세요.", 400)

    data = read_json(request)
    if data is None:
        return error("VALIDATION_ERROR", "요청 형식이 올바르지 않아요.", 400)
    question_id = data.get("question_id")
    question = None
    if is_number(question_id):
        question = Question.objects.filter(id=question_id, lesson_id=progress.lesson_id).first()
    if question is None:
        return error("VALIDATION_ERROR", "이 레슨의 문제가 아니에요.", 400)

    selected = data.get("selected")
    if not is_number(selected) or not 1 <= selected <= len(question.choices):
        return error("VALIDATION_ERROR", "선택지 번호가 올바르지 않아요.", 400)

    diagnostic = is_diagnostic(progress.lesson)
    if diagnostic and progress.attempts.filter(question=question).exists():
        return error("VALIDATION_ERROR", "진단은 문제마다 한 번만 답할 수 있어요.", 400)
    # 맞힌 문제나 해설을 본 문제는 다시 풀지 않는다 (기록이 부풀지 않게)
    if progress.attempts.filter(question=question, is_correct=True).exists():
        return error("VALIDATION_ERROR", "이미 맞힌 문제예요. 다음 문제로 넘어가 주세요.", 400)
    if progress.reveals.filter(question=question).exists():
        return error("VALIDATION_ERROR", "해설을 본 문제는 다시 풀 수 없어요. 다음 문제로 넘어가 주세요.", 400)

    is_correct = selected == question.answer
    AnswerAttempt.objects.create(
        user=progress.user,
        progress=progress,
        question=question,
        selected=selected,
        is_correct=is_correct,
    )

    # 이 풀이에서 같은 문제를 최근에 연속으로 틀린 횟수
    wrong_streak = 0
    for correct in AnswerAttempt.objects.filter(
        progress=progress, question=question
    ).order_by("-created_at", "-id").values_list("is_correct", flat=True):
        if correct:
            break
        wrong_streak += 1

    if diagnostic:
        # 진단 중에는 맞았는지도 알려 주지 않는다. 점수는 다 풀고 나서 맞힌 수로만 보여 준다
        return ok({"is_correct": None, "explanation": None, "wrong_streak": 0, "suggest_hint": False})
    return ok({
        "is_correct": is_correct,
        # 틀렸을 때는 해설(=정답)을 주지 않는다. 다시 풀거나 '해설 보기'를 고른다 (10/7 결정 A)
        "explanation": question.explanation if is_correct else None,
        "wrong_streak": wrong_streak,
        "suggest_hint": wrong_streak >= HINT_SUGGEST_STREAK,
    })


@require_POST
def reveal_explanation(request, progress_id):
    """POST /api/v1/progress/{id}/explanation : 틀린 뒤 '해설 보기' (10/7 결정 A)
    한 번 이상 답을 낸 문제만 열 수 있고, 연 뒤에는 그 문제를 다시 풀 수 없다."""
    progress = LessonProgress.objects.filter(id=progress_id).first()
    if progress is None or not owns(request, progress):
        return error("NOT_FOUND", "풀이 기록을 찾을 수 없어요.", 404)

    data = read_json(request)
    if data is None:
        return error("VALIDATION_ERROR", "요청 형식이 올바르지 않아요.", 400)
    question_id = data.get("question_id")
    question = None
    if is_number(question_id):
        question = Question.objects.filter(id=question_id, lesson_id=progress.lesson_id).first()
    if question is None:
        return error("VALIDATION_ERROR", "이 레슨의 문제가 아니에요.", 400)
    if is_diagnostic(progress.lesson):
        return error("VALIDATION_ERROR", "진단에서는 해설을 볼 수 없어요.", 400)
    if not progress.attempts.filter(question=question).exists():
        return error("VALIDATION_ERROR", "먼저 답을 하나 골라 주세요.", 400)

    # 두 번 눌러도 기록은 하나만 남는다
    ExplanationReveal.objects.get_or_create(progress=progress, question=question)
    return ok({
        "question_id": question.id,
        "answer": question.answer,  # 화면에서 정답 선택지를 표시하는 데 쓴다
        "explanation": question.explanation,
    })


@require_POST
def complete_progress(request, progress_id):
    """POST /api/v1/progress/{id}/complete : 걸린 시간과 맞힌 수를 저장한다 (FR-07, FR-11)"""
    progress = LessonProgress.objects.filter(id=progress_id).first()
    if progress is None or not owns(request, progress):
        return error("NOT_FOUND", "풀이 기록을 찾을 수 없어요.", 404)

    if progress.completed_at is None:
        question_ids = set(progress.lesson.questions.values_list("id", flat=True))
        attempts = progress.attempts.order_by("created_at", "id")
        answered = set(attempts.values_list("question_id", flat=True))
        if not question_ids <= answered:
            return error("VALIDATION_ERROR", "아직 풀지 않은 문제가 있어요.", 400)

        # 맞힌 수 = 처음 낸 답이 정답인 문제 수 (다시 골라 맞힌 것은 세지 않는다)
        first_try = {}
        for a in attempts:
            first_try.setdefault(a.question_id, a.is_correct)

        now = timezone.now()
        progress.completed_at = now
        progress.duration_sec = int((now - progress.started_at).total_seconds())
        progress.correct_count = sum(first_try.values())
        progress.save(update_fields=["completed_at", "duration_sec", "correct_count"])
    # 이미 끝낸 풀이면 저장된 값을 그대로 돌려준다 (버튼을 두 번 눌러도 기록이 바뀌지 않게)

    return ok({
        "duration_sec": progress.duration_sec,
        "correct_count": progress.correct_count,
        "question_count": progress.lesson.questions.count(),
        "week": week_summary(progress.user) if progress.user_id else None,
    })


def hints_used_today(progress):
    """오늘(한국 날짜) 이 사람이 받은 힌트 수. 게스트는 같은 브라우저(guest_key) 기준"""
    today = timezone.localdate()
    hints = HintRequest.objects.filter(created_at__date=today)
    if progress.user_id:
        return hints.filter(progress__user_id=progress.user_id).count()
    return hints.filter(progress__user__isnull=True, progress__guest_key=progress.guest_key).count()


def hint_payload(hint, used_today):
    return {
        "hint_id": hint.id,
        "level": hint.level,
        "message": hint.response,
        "sources": hint.source_lessons,
        "remaining_today": max(DAILY_LIMIT - used_today, 0),
    }


@require_POST
def request_hint(request, question_id):
    """POST /api/v1/questions/{id}/hints : 사수 힌트 한 단계 (FR-08)
    설계서 순서: 한도 확인 → 단계 순서 확인 → 근거 검색 → 모델 요청 → 정답 노출 검사 → 저장"""
    data = read_json(request)
    if data is None:
        return error("VALIDATION_ERROR", "요청 형식이 올바르지 않아요.", 400)
    progress_id, level = data.get("progress_id"), data.get("level")
    progress = LessonProgress.objects.filter(id=progress_id).first() if is_number(progress_id) else None
    if progress is None or not owns(request, progress):
        return error("NOT_FOUND", "풀이 기록을 찾을 수 없어요.", 404)
    question = Question.objects.filter(id=question_id, lesson_id=progress.lesson_id).select_related("lesson").first()
    if question is None:
        return error("NOT_FOUND", "이 레슨의 문제가 아니에요.", 404)
    if is_diagnostic(question.lesson):
        return error("VALIDATION_ERROR", "진단 중에는 사수 힌트를 쓸 수 없어요.", 400)
    if not is_number(level) or level not in (1, 2, 3):
        return error("VALIDATION_ERROR", "힌트 단계는 1~3이에요.", 400)
    if progress.completed_at is not None:
        return error("VALIDATION_ERROR", "이미 끝낸 풀이예요.", 400)
    if progress.attempts.filter(question=question, is_correct=True).exists() or \
            progress.reveals.filter(question=question).exists():
        return error("VALIDATION_ERROR", "이미 맞혔거나 해설을 본 문제예요.", 400)

    given = {h.level: h for h in progress.hints.filter(question=question)}
    if level in given:  # 같은 단계를 다시 누르면 새로 만들지 않고 그대로 돌려준다 (한도도 쓰지 않음)
        return ok(hint_payload(given[level], hints_used_today(progress)))
    if any(lv not in given for lv in range(1, level)):
        return error("HINT_LEVEL_SKIPPED", "힌트는 한 단계씩 받을 수 있어요.", 400)
    if hints_used_today(progress) >= DAILY_LIMIT:
        return error("HINT_LIMIT", "오늘 힌트를 모두 썼어요. 내일 다시 물어봐 주세요.", 429)

    previous = [given[lv].response for lv in sorted(given)]
    try:
        result = generate_hint(question, level, previous)
    except (LLMError, EmbeddingError) as e:
        # 원인은 서버 터미널(로그)에 남기고, 화면에는 레슨으로 돌아가라는 안내만 보여 준다.
        # 메시지 속 키 조각은 hide_keys로 이미 가려져 있다
        logger.warning("사수 힌트 실패 (문제 %s, %s단계): %s", question.id, level, e)
        return error("AI_UNAVAILABLE", "사수가 잠깐 자리를 비웠어요. 레슨 본문을 다시 보면 실마리가 있어요.", 503)

    try:
        hint = HintRequest.objects.create(
            user=progress.user, progress=progress, question=question, level=level,
            response=result["message"], source_lessons=result["sources"], blocked=result["blocked"],
            model=result["model"], tokens_in=result["tokens_in"], tokens_out=result["tokens_out"],
        )
    except IntegrityError:  # 버튼을 빠르게 두 번 눌러 같은 단계가 동시에 만들어진 경우
        hint = progress.hints.get(question=question, level=level)
    return ok(hint_payload(hint, hints_used_today(progress)), status=201)


# ---------- 내 설정·학습 기록 (WBS 5.1) ----------

def login_required_json(view):
    """로그인이 필요한 API. 로그인 화면으로 보내지 않고 401 JSON으로 답한다"""
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return error("NOT_AUTHENTICATED", "로그인이 필요해요.", 401)
        return view(request, *args, **kwargs)
    wrapped.__name__ = view.__name__
    wrapped.__doc__ = view.__doc__
    return wrapped


def profile_payload(profile):
    return {
        "weekly_goal_days": profile.weekly_goal_days,
        "notify_time": profile.notify_time.strftime("%H:%M") if profile.notify_time else None,
        "timezone": profile.timezone,
        "onboarded": profile.onboarded_at is not None,
        "push_devices": profile.user.push_subscriptions.count(),  # 알림을 켠 기기 수
    }


NOTIFY_TIME_RE = re.compile(r"^(0[6-9]|1\d|2[0-3]):(00|30)$")  # 06:00~23:30, 30분 단위 (예약 실행이 30분마다)


def parse_notify_time(value):
    """"HH:MM" → time. 형식이 틀리면 ValueError"""
    if not isinstance(value, str) or not NOTIFY_TIME_RE.match(value):
        raise ValueError
    hour, minute = map(int, value.split(":"))
    return dtime(hour, minute)


@require_http_methods(["GET", "PATCH"])
@login_required_json
def me(request):
    """GET/PATCH /api/v1/me : 내 설정. 지금은 주간 목표(3·5·7일)만 바꿀 수 있다 (FR-02)
    알림 시각은 5.2 알림에서 연다"""
    profile = get_profile(request.user)
    if request.method == "PATCH":
        data = read_json(request)
        if data is None:
            return error("VALIDATION_ERROR", "요청 형식이 올바르지 않아요.", 400)
        unknown = set(data) - {"weekly_goal_days", "notify_time"}
        if unknown or not data:
            return error("VALIDATION_ERROR", f"바꿀 수 없는 항목이에요: {', '.join(sorted(unknown))}", 400)
        if "weekly_goal_days" in data:
            goal = data["weekly_goal_days"]
            if not is_number(goal) or goal not in Profile.Goal.values:
                return error("VALIDATION_ERROR", "주간 목표는 3, 5, 7일 중에서 골라 주세요.", 400)
            profile.weekly_goal_days = goal
            if profile.onboarded_at is None:
                profile.onboarded_at = timezone.now()
        if "notify_time" in data:
            if data["notify_time"] is None:
                profile.notify_time = None
            else:
                try:
                    profile.notify_time = parse_notify_time(data["notify_time"])
                except ValueError:
                    return error("VALIDATION_ERROR", "알림 시각은 06:00~23:30 사이 30분 단위로 골라 주세요.", 400)
        profile.save()
    return ok(profile_payload(profile))


@require_GET
@login_required_json
def weekly_progress(request):
    """GET /api/v1/progress/weekly?weeks=4 : 최근 몇 주의 주별 달성 (FR-12). 이번 주가 맨 앞.
    가입하기 전 주는 빼고 보낸다 (갓 가입한 사람에게 0일짜리 지난주를 보여 주지 않기, 10/14 폰 점검).
    지난주들도 지금의 목표로 비교한다 (목표를 바꾼 이력은 저장하지 않는다)"""
    try:
        weeks = int(request.GET.get("weeks", 4))
    except ValueError:
        return error("VALIDATION_ERROR", "weeks는 숫자여야 해요.", 400)
    if not 1 <= weeks <= RECORD_WEEKS_MAX:
        return error("VALIDATION_ERROR", f"weeks는 1~{RECORD_WEEKS_MAX} 사이여야 해요.", 400)

    goal = get_profile(request.user).weekly_goal_days
    monday = this_monday()
    joined = timezone.localtime(request.user.date_joined).date()
    joined_monday = joined - timedelta(days=joined.weekday())
    first = max(monday - timedelta(weeks=weeks - 1), min(joined_monday, monday))
    dates = study_dates(request.user, first)
    result = []
    for i in range(weeks):
        start = monday - timedelta(weeks=i)
        if start < first:
            break
        done = sum(1 for d in dates if start <= d < start + timedelta(days=7))
        result.append({"week_start": start.isoformat(), "done_days": done, "goal_days": goal,
                       "achieved": done >= goal})
    return ok({"weeks": result})


@require_GET
@login_required_json
def lesson_records(request):
    """GET /api/v1/progress/lessons : 끝낸 레슨 목록과 예상 완료일 (FR-12)
    같은 레슨을 다시 풀었으면 가장 최근 기록 하나만 보여 준다. 진단과 공개 전 초안은 뺀다"""
    lessons = list(published_lessons().annotate(question_count=Count("questions")))
    latest = {}
    for p in (LessonProgress.objects.filter(user=request.user, completed_at__isnull=False,
                                            lesson__in=lessons)
              .order_by("completed_at")):
        latest[p.lesson_id] = p  # 시간순으로 덮어써서 마지막(최근) 것이 남는다

    records = [
        {
            **lesson_brief(lesson),
            "completed_at": timezone.localtime(latest[lesson.id].completed_at).isoformat(),  # 한국 시간
            "correct_count": latest[lesson.id].correct_count,
            "question_count": lesson.question_count,
        }
        for lesson in lessons if lesson.id in latest
    ]
    remaining = len(lessons) - len(records)
    return ok({
        "lessons": records,
        "total": len(lessons),
        "remaining": remaining,
        "estimate": estimate_finish(remaining, get_profile(request.user).weekly_goal_days),
    })


@require_POST
@login_required_json
def skip_pre_diagnostic(request):
    """POST /api/v1/diagnostics/pre/skip : 사전 진단 건너뛰기. 건너뛴 사람은 베타 분석에서 뺀다"""
    profile = get_profile(request.user)
    if profile.pre_diag_skipped_at is None:
        profile.pre_diag_skipped_at = timezone.now()
        profile.save(update_fields=["pre_diag_skipped_at", "updated_at"])
    return ok({"skipped": True})


# ---------- 웹 푸시 알림 (WBS 5.2, FR-14) ----------

MAX_PUSH_DEVICES = 5
TEST_PUSH_COOLDOWN = 30  # 초. 테스트 알림 버튼 연타 방지


@require_GET
def push_key(request):
    """GET /api/v1/push/key : 브라우저가 구독할 때 쓰는 서버 공개 키. 서버 설정이 없으면 null (화면에서 알림 카드를 숨긴다)"""
    return ok({"public_key": webpush.vapid_public_key_b64()})


@require_POST
@login_required_json
def push_subscribe(request):
    """POST /api/v1/push/subscriptions : 이 기기에서 알림 켜기 {endpoint, keys:{p256dh, auth}, notify_time}"""
    data = read_json(request)
    if data is None:
        return error("VALIDATION_ERROR", "요청 형식이 올바르지 않아요.", 400)
    endpoint = data.get("endpoint")
    keys = data.get("keys") if isinstance(data.get("keys"), dict) else {}
    if not isinstance(endpoint, str) or len(endpoint) > 1000 or not webpush.is_allowed_endpoint(endpoint):
        return error("VALIDATION_ERROR", "지원하지 않는 알림 주소예요.", 400)
    try:
        p256dh, auth = webpush.b64u_decode(keys.get("p256dh", "")), webpush.b64u_decode(keys.get("auth", ""))
    except (ValueError, TypeError):
        p256dh = auth = b""
    if len(p256dh) != 65 or p256dh[0] != 4 or len(auth) != 16:
        return error("VALIDATION_ERROR", "알림 키 형식이 올바르지 않아요.", 400)

    profile = get_profile(request.user)
    if "notify_time" in data:
        try:
            profile.notify_time = parse_notify_time(data["notify_time"])
        except ValueError:
            return error("VALIDATION_ERROR", "알림 시각은 06:00~23:30 사이 30분 단위로 골라 주세요.", 400)
        profile.save(update_fields=["notify_time", "updated_at"])

    # 같은 기기를 다른 계정으로 다시 구독하면 지금 계정으로 옮긴다
    PushSubscription.objects.update_or_create(endpoint=endpoint, defaults={
        "user": request.user, "p256dh": keys["p256dh"], "auth": keys["auth"],
        "user_agent": request.META.get("HTTP_USER_AGENT", "")[:300],
    })
    extra = request.user.push_subscriptions.order_by("-created_at").values_list("id", flat=True)[MAX_PUSH_DEVICES:]
    PushSubscription.objects.filter(id__in=list(extra)).delete()
    return ok(profile_payload(profile), status=201)


@require_POST
@login_required_json
def push_unsubscribe(request):
    """POST /api/v1/push/subscriptions/delete : 이 기기 알림 끄기 {endpoint}. 남은 기기가 없으면 알림 시각도 지운다"""
    data = read_json(request) or {}
    request.user.push_subscriptions.filter(endpoint=data.get("endpoint", "")).delete()
    profile = get_profile(request.user)
    if not request.user.push_subscriptions.exists() and profile.notify_time is not None:
        profile.notify_time = None
        profile.save(update_fields=["notify_time", "updated_at"])
    return ok(profile_payload(profile))


@require_POST
@login_required_json
def push_test(request):
    """POST /api/v1/push/test : 내 기기로 테스트 알림 (폰 점검용). 30초에 한 번"""
    if webpush.vapid_private_key() is None:
        return error("PUSH_DISABLED", "서버에 알림 설정이 아직 없어요.", 503)
    if not request.user.push_subscriptions.exists():
        return error("VALIDATION_ERROR", "먼저 이 기기에서 알림을 켜 주세요.", 400)
    if not cache.add(f"push-test:{request.user.id}", 1, TEST_PUSH_COOLDOWN):
        return error("RATE_LIMITED", "테스트 알림은 30초에 한 번 보낼 수 있어요.", 429)
    sent, removed = push.send_to_user(request.user, {
        "title": "코드사수", "body": "알림이 잘 도착했어요. 정한 시간에 오늘의 레슨을 알려 드릴게요.", "url": "/",
    })
    return ok({"sent": sent, "removed": removed})


@require_POST
@login_required_json
def push_opened(request):
    """POST /api/v1/push/opened : 알림을 눌러 앱을 열었다 (베타 분석: 알림 → 학습 전환)"""
    today = timezone.localdate()
    PushDelivery.objects.filter(user=request.user, sent_on=today, opened_at__isnull=True).update(opened_at=timezone.now())
    return ok({"ok": True})


@csrf_exempt  # 브라우저가 아니라 Cloud Scheduler가 부른다. 대신 비밀 토큰으로 확인한다
@require_POST
def push_send_due(request):
    """POST /internal/push/send-due : 지금 알림을 보낼 사람에게 보낸다 (Cloud Scheduler가 30분마다 호출)"""
    expected = os.environ.get("PUSH_CRON_TOKEN", "")
    if not expected or webpush.vapid_private_key() is None:
        return error("PUSH_DISABLED", "알림 설정이 없어요.", 503)
    given = request.headers.get("X-Cron-Token", "")
    if not hmac.compare_digest(given.encode(), expected.encode()):
        return error("FORBIDDEN", "권한이 없어요.", 403)
    summary = push.send_due()
    logger.info("하루 알림: %s", summary)
    return ok(summary)
