"""레슨 풀이 API (설계 탭 'API 명세'의 레슨 영역 5개)

- 응답은 JSON, 에러는 {"error": {"code", "message"}} 한 가지 모양으로 보낸다.
- 정답(answer)은 '해설 보기'를 누른 뒤에만 보낸다. 채점은 서버에서만 한다.
- 로그인 없이는 첫 레슨만 풀 수 있다(FR-01). 게스트 기록은 세션의 guest_key로 묶는다.
"""

import json
import logging
import secrets
from datetime import timedelta

from django.db import IntegrityError
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .embeddings import EmbeddingError
from .hints import DAILY_LIMIT, LLMError, generate_hint
from .models import AnswerAttempt, ExplanationReveal, HintRequest, Lesson, LessonProgress, Question

logger = logging.getLogger(__name__)

DEFAULT_WEEKLY_GOAL_DAYS = 5  # 5.1에서 사용자가 고른 주간 목표(profile)로 바꾼다
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


def week_summary(user):
    """이번 주(월요일 시작, 한국 시간) 레슨을 끝낸 날 수. 하루에 여러 개 끝내도 1일 (FR-11)."""
    today = timezone.localdate()
    monday = today - timedelta(days=today.weekday())
    completed = LessonProgress.objects.filter(
        user=user, completed_at__date__gte=monday
    ).values_list("completed_at", flat=True)
    done_days = {timezone.localtime(t).date() for t in completed}
    return {"done_days": len(done_days), "goal_days": DEFAULT_WEEKLY_GOAL_DAYS}


def lesson_brief(lesson):
    return {
        "id": lesson.id,
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
    return ok({
        "lesson": lesson_brief(lesson) if lesson else None,
        "week": week_summary(request.user),
        "needs_onboarding": False,  # 5.1(profile) 전까지는 항상 False
        "is_guest": False,
    })


@require_GET
def lesson_detail(request, lesson_id):
    """GET /api/v1/lessons/{id} : 레슨 본문과 문제. 정답·해설은 보내지 않는다 (FR-05)"""
    lesson = published_lessons().filter(id=lesson_id).first()
    if lesson is None:
        return error("NOT_FOUND", "레슨을 찾을 수 없어요.", 404)
    if not can_open(request, lesson):
        return error("NOT_AUTHENTICATED", "로그인하면 다음 레슨을 풀 수 있어요.", 401)

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
    })


@require_POST
def start_progress(request, lesson_id):
    """POST /api/v1/lessons/{id}/progress : 풀이 시작. 다시 풀면 새 기록 (FR-07)"""
    lesson = published_lessons().filter(id=lesson_id).first()
    if lesson is None:
        return error("NOT_FOUND", "레슨을 찾을 수 없어요.", 404)
    if not can_open(request, lesson):
        return error("NOT_AUTHENTICATED", "로그인하면 다음 레슨을 풀 수 있어요.", 401)

    data = read_json(request)
    if data is None:
        return error("VALIDATION_ERROR", "요청 형식이 올바르지 않아요.", 400)
    phase = data.get("phase", "")
    if phase not in ("", *LessonProgress.Phase.values):
        return error("VALIDATION_ERROR", "phase는 pre 또는 post만 쓸 수 있어요.", 400)

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
