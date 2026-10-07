"""python manage.py hint_check : 실제 모델로 힌트를 만들어 품질을 점검한다 (WBS 4.2·4.3)

공개된 문제마다 1→2→3단계 힌트를 만들어 보여 주고, 정답 노출 검사 결과를 요약한다.
DB에는 저장하지 않고 하루 한도에도 들어가지 않는다. OpenAI 요금은 실제로 나간다.
- 특정 문제만: --question 1
"""

from django.core.management.base import BaseCommand, CommandError

from learning.embeddings import EmbeddingError
from learning.hints import LEVELS, generate_hint
from learning.llm import LLMError, hint_model
from learning.models import Question


class Command(BaseCommand):
    help = "실제 모델로 문제별 3단계 힌트를 만들어 정답 노출·근거를 점검한다 (저장하지 않음)"

    def add_arguments(self, parser):
        parser.add_argument("--question", type=int, help="이 문제 번호(id)만 점검")

    def handle(self, *args, **options):
        questions = Question.objects.filter(
            lesson__is_published=True, lesson__course__is_published=True
        ).select_related("lesson").order_by("lesson__order_no", "order_no")
        if options["question"]:
            questions = questions.filter(id=options["question"])
        if not questions:
            raise CommandError("점검할 문제가 없어요.")

        self.stdout.write(f"모델: {hint_model()}\n")
        total = blocked = retried = tokens_in = tokens_out = 0
        for q in questions:
            self.stdout.write(f"\n■ 문제 {q.id} ({q.lesson.order_no}-{q.order_no} {q.get_qtype_display()}) "
                              f"정답 {q.answer}번: {q.choices[q.answer - 1]}")
            previous = []
            for level in (1, 2, 3):
                try:
                    r = generate_hint(q, level, previous)
                except (LLMError, EmbeddingError) as e:
                    raise CommandError(str(e))
                total += 1
                blocked += r["blocked"]
                retried += r["attempts"] > 1
                tokens_in += r["tokens_in"]
                tokens_out += r["tokens_out"]
                flag = "⛔ 고정 문구로 막음" if r["blocked"] else ("↻ 한 번 다시 만듦" if r["attempts"] > 1 else "통과")
                sources = ", ".join(s["title"] for s in r["sources"])
                self.stdout.write(f"  {level}단계·{LEVELS[level][0]} [{flag}] {r['message']}")
                self.stdout.write(f"     근거: {sources}" + (f" | 걸린 이유: {', '.join(r['leaks'])}" if r["leaks"] else ""))
                previous.append(r["message"])

        self.stdout.write(self.style.SUCCESS(
            f"\n힌트 {total}개 · 고정 문구로 막음 {blocked}개 · 다시 만든 것 {retried}개 · "
            f"입력 토큰 {tokens_in} · 출력 토큰 {tokens_out}(생각 토큰 포함)"
        ))
