"""python manage.py funnel : 레슨별 단계별 이탈 리포트 (WBS 6.1, 베타 분석 6.4)

레슨마다 사람 수를 단계별로 센다: 열람 → 풀이 시작 → 문제 1·2·3에 답 → 완료.
- 사람 = 계정(가입 전 게스트 기록은 가입하면 계정으로 옮겨진다). 게스트는 브라우저(guest_key) 하나를 한 명으로 센다.
- 같은 사람이 여러 번 열거나 다시 풀어도 한 번으로 센다.
- 관리자(is_staff)와 --exclude로 준 이메일(테스트 계정)은 뺀다.
- 기간: --since 2026-11-09 (한국 날짜, 그날 0시부터)

예) python manage.py funnel --since 2026-11-09 --exclude a@example.com,b@example.com
"""

from datetime import datetime, time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from learning.models import AnswerAttempt, Lesson, LessonProgress, LessonView


def person(user_id, guest_key):
    return f"u{user_id}" if user_id else f"g{guest_key}"


class Command(BaseCommand):
    help = "레슨별 단계별 이탈(열람 → 시작 → 문제별 답 → 완료)을 사람 수로 보여 준다"

    def add_arguments(self, parser):
        parser.add_argument("--since", help="이 날짜(한국 시간, YYYY-MM-DD)부터의 기록만")
        parser.add_argument("--exclude", default="", help="뺄 계정 이메일, 쉼표로 구분 (테스트 계정)")

    def handle(self, *args, **opts):
        since = None
        if opts["since"]:
            try:
                day = datetime.strptime(opts["since"], "%Y-%m-%d").date()
            except ValueError:
                raise CommandError("--since는 2026-11-09 형식이어야 해요.")
            since = timezone.make_aware(datetime.combine(day, time.min))
        excluded = [e.strip() for e in opts["exclude"].split(",") if e.strip()]

        def scope(qs, user_field, date_field):
            qs = qs.exclude(**{f"{user_field}__is_staff": True})
            if excluded:
                qs = qs.exclude(**{f"{user_field}__email__in": excluded})
            if since:
                qs = qs.filter(**{f"{date_field}__gte": since})
            return qs

        views = scope(LessonView.objects.all(), "user", "created_at")
        progresses = scope(LessonProgress.objects.all(), "user", "started_at")
        attempts = scope(AnswerAttempt.objects.all(), "progress__user", "created_at")

        lessons = (Lesson.objects.filter(is_published=True, course__is_published=True)
                   .prefetch_related("questions").order_by("course_id", "kind", "order_no"))
        self.stdout.write("레슨별 단계별 이탈 (사람 수, 괄호는 바로 앞 단계 대비 남은 비율)")
        if since:
            self.stdout.write(f"기간: {opts['since']}부터")
        if excluded:
            self.stdout.write(f"제외 계정: {len(excluded)}개 + 관리자")
        total_people = set()

        for lesson in lessons:
            viewed = {person(u, g) for u, g in views.filter(lesson=lesson).values_list("user_id", "guest_key")}
            started_rows = progresses.filter(lesson=lesson)
            started = {person(u, g) for u, g in started_rows.values_list("user_id", "guest_key")}
            done = {person(u, g) for u, g in
                    started_rows.filter(completed_at__isnull=False).values_list("user_id", "guest_key")}
            steps = [("열람", viewed), ("시작", started)]
            for q in sorted(lesson.questions.all(), key=lambda q: q.order_no):
                answered = {person(u, g) for u, g in attempts.filter(question=q)
                            .values_list("progress__user_id", "progress__guest_key")}
                steps.append((f"{q.order_no}번 답", answered))
            steps.append(("완료", done))
            if not any(people for _, people in steps):
                continue
            total_people |= viewed | started

            name = f"진단 {lesson.title[-1]}" if lesson.kind == Lesson.Kind.DIAGNOSTIC else f"L{lesson.order_no:02d}"
            parts, prev, worst = [], None, None
            for label, people in steps:
                n = len(people)
                if prev is None:
                    parts.append(f"{label} {n}")
                else:
                    rate = f"{n * 100 // prev}%" if prev else "-"
                    parts.append(f"{label} {n}({rate})")
                    lost = prev - n
                    if prev and (worst is None or lost > worst[1]):
                        worst = (label, lost)
                prev = n
            line = f"{name} " + " → ".join(parts)
            if worst and worst[1] > 0:
                line += f"   가장 많이 빠진 곳: {worst[0]} 앞 ({worst[1]}명)"
            self.stdout.write(line)

        self.stdout.write(f"레슨을 연 사람(중복 없이): {len(total_people)}명")
