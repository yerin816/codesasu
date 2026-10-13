"""python manage.py sync_lessons : lessons/ 폴더의 레슨 파일을 DB에 반영한다 (WBS 2.4)

레슨 내용의 원본은 파일이다. 서버가 켜질 때마다 돌아서, 파일을 고쳐 push하면 배포와 함께 반영된다.
- 과정은 slug, 레슨은 (과정, 번호), 문제는 (레슨, 번호)로 찾아 바뀐 것만 고친다.
  행을 지우고 새로 만들지 않으므로 사용자의 풀이 기록은 그대로 이어진다.
- 파일에 없는 레슨·문제는 지우지 않고 알려만 준다. (푼 기록이 없는 문제는 지운다)
- 형식 검사에 걸리면 아무것도 바꾸지 않고 멈춘다. 코드 실행 검사는 배포 빌드에서 이미 했다.
- 이어서 힌트 검색용 청크를 맞춘다(ingest_chunks: 제목·본문이 바뀐 레슨만 임베딩, --no-chunks로 끔).
  지난번 임베딩이 실패했어도 다음 시작 때 다시 시도된다.
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from learning.lesson_files import LESSONS_DIR, check_all
from learning.models import Course, Lesson, Question

LESSON_FIELDS = ["kind", "track", "title", "body_md", "est_minutes", "is_published"]
QUESTION_FIELDS = ["qtype", "prompt", "code", "choices", "answer", "explanation"]


def _name(ld):
    """로그에 쓰는 이름: 레슨은 L03, 진단은 '진단 A'"""
    if ld.kind == "diagnostic":
        return ld.path.stem.replace("diag_", "진단 ")
    return f"L{ld.order_no:02d}"


def _apply(obj, values):
    """바뀐 필드 이름 목록을 돌려주고 obj에 값을 넣는다"""
    changed = [k for k, v in values.items() if getattr(obj, k) != v]
    for k in changed:
        setattr(obj, k, values[k])
    return changed


class Command(BaseCommand):
    help = "lessons/ 폴더의 레슨 파일을 DB에 반영한다"

    def add_arguments(self, parser):
        parser.add_argument("--dir", default=str(LESSONS_DIR))
        parser.add_argument("--dry-run", action="store_true", help="바뀔 내용만 보여 주고 저장하지 않는다")
        parser.add_argument("--no-chunks", action="store_true", help="힌트 검색용 청크를 다시 만들지 않는다")

    def handle(self, *args, **opts):
        courses, report = check_all(opts["dir"], run=False)
        if not report.ok:
            raise CommandError("레슨 파일 검사 실패 - DB는 그대로:\n" + "\n".join(report.errors))

        self.stats = {"created": 0, "updated": 0, "same": 0}
        self.notes = []
        try:
            with transaction.atomic():
                for c in courses:
                    self._sync_course(c)
                if opts["dry_run"]:
                    raise _DryRun
        except _DryRun:
            pass

        for note in self.notes:
            self.stdout.write(note)
        s = self.stats
        prefix = "미리보기" if opts["dry_run"] else "레슨 반영"
        self.stdout.write(f"{prefix}: 새로 {s['created']}개, 바뀜 {s['updated']}개, 그대로 {s['same']}개 (레슨·문제 합계)")

        if not opts["dry_run"] and not opts["no_chunks"]:
            try:
                call_command("ingest_chunks", stdout=self.stdout)
            except Exception as e:  # noqa: BLE001 - 청크 실패로 서버가 안 켜지면 안 된다
                self.stdout.write(f"청크 갱신 실패(힌트는 예전 청크로 동작): {e}")
        return None

    def _count(self, created, changed, label):
        if created:
            self.stats["created"] += 1
            self.notes.append(f"  + {label}")
        elif changed:
            self.stats["updated"] += 1
            self.notes.append(f"  ~ {label}: {', '.join(changed)}")
        else:
            self.stats["same"] += 1

    def _sync_course(self, data):
        course, created = Course.objects.get_or_create(
            slug=data.slug, defaults={"title": data.title, "language": data.language, "is_published": data.is_published}
        )
        changed = [] if created else _apply(course, {"title": data.title, "language": data.language,
                                                      "is_published": data.is_published})
        if changed:
            course.save()

        for ld in data.lessons:
            values = {"kind": ld.kind, "track": ld.track, "title": ld.title, "body_md": ld.body_md,
                      "est_minutes": ld.est_minutes, "is_published": ld.is_published}
            lesson = Lesson.objects.filter(course=course, order_no=ld.order_no).first()
            if lesson is None:
                lesson = Lesson.objects.create(course=course, order_no=ld.order_no, **values)
                self._count(True, [], f"{_name(ld)} {ld.title}")
            else:
                changed = _apply(lesson, values)
                if changed:
                    lesson.save()
                self._count(False, changed, f"{_name(ld)} {ld.title}")
            self._sync_questions(lesson, ld)

        file_numbers = {ld.order_no for ld in data.lessons}
        for extra in course.lessons.exclude(order_no__in=file_numbers):
            self.notes.append(f"  ! {extra.order_no}번 {extra.title}: 파일에 없음 - 그대로 둠")

    def _sync_questions(self, lesson, ld):
        for qd in ld.questions:
            values = {"qtype": qd.qtype, "prompt": qd.prompt, "code": qd.code, "choices": qd.choices,
                      "answer": qd.answer, "explanation": qd.explanation}
            label = f"{_name(ld)} 문제{qd.order_no}"
            question = Question.objects.filter(lesson=lesson, order_no=qd.order_no).first()
            if question is None:
                Question.objects.create(lesson=lesson, order_no=qd.order_no, **values)
                self._count(True, [], label)
                continue
            changed = _apply(question, values)
            if changed:
                if "answer" in changed and question.attempts.exists():
                    self.notes.append(f"  ! {label}: 정답 번호가 바뀜 - 예전 풀이의 정답 여부는 바뀐 정답과 다를 수 있음")
                question.save()
            self._count(False, changed, label)

        for extra in lesson.questions.exclude(order_no__in=[q.order_no for q in ld.questions]):
            if extra.attempts.exists() or extra.hints.exists() or extra.reveals.exists():
                self.notes.append(f"  ! {_name(ld)} 문제{extra.order_no}: 파일에 없지만 푼 기록이 있어 그대로 둠 "
                                  "(관리자 화면에서 지우거나 파일에 다시 넣어 주세요)")
            else:
                extra.delete()
                self.notes.append(f"  - {_name(ld)} 문제{extra.order_no}: 파일에 없어 지움(푼 기록 없음)")


class _DryRun(Exception):
    pass
