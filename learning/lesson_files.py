"""레슨 파일(lessons/<과정>/L01.toml …)을 읽고 검사한다 (WBS 2.4 레슨 파이프라인)

Django와 DB 없이 돈다. 그래서 PC에서도, 배포 빌드 단계에서도 같은 검사를 한다.

    python -m learning.lesson_files            검사 (pandas가 없으면 그 문제만 건너뜀)
    python -m learning.lesson_files --strict   건너뛴 문제도 실패로 본다 (배포 빌드에서 사용)

검사하는 것
- 형식: 트랙·유형 이름, 선택지 4개(중복 없음), 정답 번호, 레슨당 문제 2~3개, 레슨 번호 1부터 빈칸 없이
- 폰 화면: 코드 한 줄 44칸(한글 2칸) 이하
- 줄 번호: 선택지·해설에 적힌 'N번째 줄'이 코드 줄 수 안에 있는지
- 실행: 문제 코드를 실제로 돌려
    결과 예측 → 출력이 정답 선택지와 같은지
    에러 해석 → 적어 둔 에러가 정말 나는지
    고칠 줄   → 고치기 전엔 틀리고, 정답대로 고치면 기대한 결과가 나오는지,
                오답 선택지대로 고치면 여전히 틀리는지
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

LESSONS_DIR = Path(__file__).resolve().parent.parent / "lessons"

TRACKS = {"reading": "읽기", "error": "에러", "shipping": "내보내기"}
QTYPES = {"predict": "결과 예측", "error": "에러 해석", "fix_line": "고칠 줄", "flow": "흐름 설명"}
KINDS = {"lesson", "diagnostic"}

# 문제 코드 한 줄의 최대 폭. 한글은 영문 2칸으로 센다.
# 10/8 아이폰 확인에서 긴 줄이 잘려 정답을 가르는 조건이 안 보였다.
# 폭 375px 화면은 글자를 11.5px까지 줄여도 약 45칸이 들어가서 44칸으로 정했다.
CODE_MAX_WIDTH = 44
CODE_MAX_LINES = 16  # 넘으면 경고: 폰 한 화면에 코드와 선택지가 같이 안 보인다
RUN_TIMEOUT_SEC = 20
SYNTAX_ERRORS = {"SyntaxError", "IndentationError", "TabError"}
LESSON_KEYS = {"order_no", "kind", "track", "title", "est_minutes", "published", "body", "questions"}
QUESTION_KEYS = {"qtype", "prompt", "code", "choices", "answer", "explanation", "check"}
CHECK_KEYS = {"run", "why", "error", "line", "fix", "expect", "wrong", "files"}
COURSE_KEYS = {"slug", "title", "language", "published"}
_LINE_REF = re.compile(r"(\d+)\s*(?:~\s*(\d+)\s*)?번째\s*줄")


def display_width(line):
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in line)


def long_code_lines(code, limit=CODE_MAX_WIDTH):
    """폭을 넘는 줄 번호(1부터) 목록"""
    return [i for i, line in enumerate(code.splitlines(), 1) if display_width(line) > limit]


# ---------------------------------------------------------------- 읽기

@dataclass
class QuestionData:
    order_no: int
    qtype: str
    prompt: str
    code: str
    choices: list
    answer: int
    explanation: str
    check: dict = field(default_factory=dict)
    unknown_keys: list = field(default_factory=list)


@dataclass
class LessonData:
    path: Path
    order_no: int
    kind: str
    track: str
    title: str
    body_md: str
    est_minutes: int
    is_published: bool
    questions: list
    unknown_keys: list = field(default_factory=list)


@dataclass
class CourseData:
    path: Path
    slug: str
    title: str
    language: str
    is_published: bool
    lessons: list
    unknown_keys: list = field(default_factory=list)


def _unknown(raw, allowed, prefix=""):
    # 오타(publshed 등)나 잘못된 위치의 [check]는 조용히 무시되면 안 된다
    return [prefix + k for k in raw if k not in allowed] if isinstance(raw, dict) else []


class LessonFileError(Exception):
    pass


def _text(value):
    # TOML의 ''' 블록은 맨 끝 줄바꿈까지 담는다. DB에는 끝 공백 없이 넣는다
    return value.strip("\n") if isinstance(value, str) else value


def _load_toml(path):
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise LessonFileError(f"{path.name}: 파일 형식 오류 - {e}") from e


def load_course(course_dir):
    course_dir = Path(course_dir)
    meta = _load_toml(course_dir / "course.toml")
    lessons = []
    for path in sorted(course_dir.glob("*.toml")):
        if path.name == "course.toml":
            continue
        raw = _load_toml(path)
        questions = [
            QuestionData(
                order_no=i,
                qtype=q.get("qtype", ""),
                prompt=_text(q.get("prompt", "")),
                code=_text(q.get("code", "")),
                choices=q.get("choices", []),
                answer=q.get("answer"),
                explanation=_text(q.get("explanation", "")),
                check=q.get("check", {}),
                unknown_keys=_unknown(q, QUESTION_KEYS) + _unknown(q.get("check", {}), CHECK_KEYS, "check."),
            )
            for i, q in enumerate(raw.get("questions", []), 1)
        ]
        lessons.append(LessonData(
            path=path,
            order_no=raw.get("order_no"),
            kind=raw.get("kind", "lesson"),
            track=raw.get("track", ""),
            title=_text(raw.get("title", "")),
            body_md=_text(raw.get("body", "")),
            est_minutes=raw.get("est_minutes", 10),
            is_published=raw.get("published", False),
            questions=questions,
            unknown_keys=_unknown(raw, LESSON_KEYS),
        ))
    lessons.sort(key=lambda l: (l.order_no if isinstance(l.order_no, int) else 10**6, l.path.name))
    return CourseData(
        path=course_dir,
        slug=meta.get("slug", course_dir.name),
        title=meta.get("title", ""),
        language=meta.get("language", "python"),
        is_published=meta.get("published", False),
        lessons=lessons,
        unknown_keys=_unknown(meta, COURSE_KEYS),
    )


def load_all(root=LESSONS_DIR):
    root = Path(root)
    return [load_course(d) for d in sorted(root.iterdir()) if (d / "course.toml").exists()]


# ---------------------------------------------------------------- 실행

@dataclass
class RunResult:
    output: str = ""
    error: str = ""  # 에러 종류 이름. 없으면 ""
    error_line: str = ""  # Traceback 마지막 줄
    skipped: str = ""  # 실행하지 못한 이유


def _normalize(text):
    lines = text.replace("\r\n", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip()


def _missing_module(code):
    for name in ("pandas", "openpyxl"):
        if re.search(rf"^\s*(import|from)\s+{name}\b", code, re.M) or (name == "pandas" and "pd.read_excel" in code):
            try:
                __import__(name)
            except ImportError:
                return name
    return ""


def run_code(code, files=None):
    """임시 폴더에서 코드를 따로 실행해 출력과 에러를 돌려준다."""
    missing = _missing_module(code)
    if missing:
        return RunResult(skipped=f"{missing}가 설치돼 있지 않음")
    with tempfile.TemporaryDirectory() as tmp:
        for f in files or []:
            target = Path(tmp) / f["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_text(f.get("text", "")).encode(f.get("encoding", "utf-8")))
        script = Path(tmp) / "main.py"
        script.write_text(code + "\n", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE")}
        env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-X", "utf8", str(script)], cwd=tmp, env=env,
                capture_output=True, timeout=RUN_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            return RunResult(error="Timeout", error_line=f"{RUN_TIMEOUT_SEC}초 안에 끝나지 않음")
    out = _normalize(proc.stdout.decode("utf-8", "replace"))
    if proc.returncode == 0:
        return RunResult(output=out)
    last = _normalize(proc.stderr.decode("utf-8", "replace")).split("\n")[-1]
    name = last.split(":", 1)[0].strip().rsplit(".", 1)[-1]
    return RunResult(output=out, error=name, error_line=last)


def apply_fix(code, line, new_text):
    """line(정수 또는 [시작, 끝])번째 줄을 new_text로 바꾼 코드"""
    lines = code.split("\n")
    start, end = (line, line) if isinstance(line, int) else (line[0], line[1])
    if not (1 <= start <= end <= len(lines)):
        raise LessonFileError(f"고칠 줄 {line}이 코드 줄 수({len(lines)})를 벗어남")
    return "\n".join(lines[: start - 1] + _text(new_text).split("\n") + lines[end:])


def _describe(result):
    if result.skipped:
        return f"(실행 못 함: {result.skipped})"
    if result.error:
        return result.error_line or result.error
    return repr(result.output)


# ---------------------------------------------------------------- 검사

@dataclass
class Report:
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    lines: list = field(default_factory=list)  # 사람이 읽는 진행 기록

    @property
    def ok(self):
        return not self.errors


def _check_question(where, q, report, run):
    if q.unknown_keys:
        report.errors.append(f"{where}: 모르는 항목 {q.unknown_keys} (오타인지 확인)")
    n_choices = len(q.choices) if isinstance(q.choices, list) else 0
    if q.qtype not in QTYPES:
        report.errors.append(f"{where}: 유형 '{q.qtype}'은 없음 ({', '.join(QTYPES)} 중 하나)")
        return
    if not q.prompt:
        report.errors.append(f"{where}: 질문(prompt)이 비어 있음")
    if not q.explanation:
        report.errors.append(f"{where}: 해설(explanation)이 비어 있음")
    if n_choices != 4 or not all(isinstance(c, str) and c.strip() for c in q.choices):
        report.errors.append(f"{where}: 선택지는 빈칸 없는 글자 4개여야 함 (지금 {n_choices}개)")
    elif len(set(c.strip() for c in q.choices)) != 4:
        report.errors.append(f"{where}: 선택지에 같은 내용이 있음")
    if not isinstance(q.answer, int) or isinstance(q.answer, bool) or not 1 <= q.answer <= max(n_choices, 1):
        report.errors.append(f"{where}: 정답 번호 {q.answer!r}가 1~{n_choices} 밖")
        return

    code_lines = q.code.split("\n") if q.code else []
    wide = long_code_lines(q.code)
    if wide:
        report.errors.append(f"{where}: 폰에서 잘리는 줄 {wide} (한 줄 {CODE_MAX_WIDTH}칸, 한글 2칸)")
    if len(code_lines) > CODE_MAX_LINES:
        report.warnings.append(f"{where}: 코드가 {len(code_lines)}줄 (폰 한 화면은 {CODE_MAX_LINES}줄 이하 권장)")

    # 선택지·해설·질문에 적은 'N번째 줄'이 코드 안에 있는지 (코드를 고친 뒤 줄 번호가 어긋나는 실수를 잡는다)
    for text in [q.prompt, q.explanation, *q.choices]:
        for a, b in _LINE_REF.findall(text):
            if int(b or a) > len(code_lines):
                report.errors.append(f"{where}: '{a}{'~' + b if b else ''}번째 줄'이 있는데 코드는 {len(code_lines)}줄")

    check = q.check or {}
    expected_error = check.get("error", "")
    if q.code and expected_error not in SYNTAX_ERRORS:
        try:
            compile(q.code, "main.py", "exec")
        except SyntaxError as e:
            report.errors.append(f"{where}: 코드 문법 오류 {e.lineno}번째 줄 - {e.msg}")
            return

    correct = q.choices[q.answer - 1]
    if q.qtype == "flow" or check.get("run") is False:
        why = "흐름 문제" if q.qtype == "flow" else check.get("why", "")
        if q.qtype != "flow" and not why:
            report.errors.append(f"{where}: run = false면 이유(why)를 적어야 함")
        report.lines.append(f"    {q.order_no} {QTYPES[q.qtype]}: 실행 안 함 ({why})")
        return
    if not run:
        report.lines.append(f"    {q.order_no} {QTYPES[q.qtype]}: 형식만 확인")
        return

    files = check.get("files", [])

    def skipped(result):
        if result.skipped:
            report.skipped.append(f"{where}: {result.skipped}")
            report.lines.append(f"    {q.order_no} {QTYPES[q.qtype]}: 건너뜀 ({result.skipped})")
            return True
        return False

    if q.qtype == "predict":
        result = run_code(q.code, files)
        if skipped(result):
            return
        if result.error:
            report.errors.append(f"{where}: 실행하니 에러 - {result.error_line}")
        elif result.output != _normalize(correct):
            others = [i for i, c in enumerate(q.choices, 1) if _normalize(c) == result.output]
            hint = f" (실제 출력은 {others[0]}번 선택지)" if others else ""
            report.errors.append(f"{where}: 실제 출력 {result.output!r} ≠ 정답 {q.answer}번 {correct!r}{hint}")
        else:
            report.lines.append(f"    {q.order_no} 결과 예측: OK 실제 출력 {result.output!r} = 정답 {q.answer}번")

    elif q.qtype == "error":
        if not expected_error:
            report.errors.append(f"{where}: 에러 문제는 check.error(예: \"KeyError\")를 적어야 함")
            return
        result = run_code(q.code, files)
        if skipped(result):
            return
        if result.error != expected_error:
            report.errors.append(f"{where}: {expected_error}가 나야 하는데 실제는 {_describe(result)}")
        else:
            report.lines.append(f"    {q.order_no} 에러 해석: OK 실제로 {result.error_line}")

    elif q.qtype == "fix_line":
        need = [k for k in ("line", "fix", "expect") if k not in check]
        if need:
            report.errors.append(f"{where}: 고칠 줄 문제는 check.{', check.'.join(need)}가 필요함")
            return
        expect = _normalize(check["expect"])
        before = run_code(q.code, files)
        if skipped(before):
            return
        try:
            after = run_code(apply_fix(q.code, check["line"], check["fix"]), files)
        except LessonFileError as e:
            report.errors.append(f"{where}: {e}")
            return
        problems = []
        if not before.error and before.output == expect:
            problems.append("고치기 전에도 이미 기대 결과가 나옴")
        if after.error or after.output != expect:
            problems.append(f"정답대로 고쳐도 {_describe(after)} (기대 {expect!r})")
        wrong_notes = []
        for alt in check.get("wrong", []):
            try:
                alt_result = run_code(apply_fix(q.code, alt["line"], alt["fix"]), files)
            except (LessonFileError, KeyError) as e:
                problems.append(f"오답 {alt.get('choice')}번 설정 오류 {e}")
                continue
            if alt.get("choice") == q.answer:
                problems.append("check.wrong에 정답 번호가 들어 있음")
            elif not alt_result.error and alt_result.output == expect:
                problems.append(f"오답 {alt['choice']}번대로 고쳐도 기대 결과가 나옴 (정답이 2개)")
            else:
                wrong_notes.append(f"오답 {alt['choice']}번 → {_describe(alt_result)}")
        if problems:
            report.errors.extend(f"{where}: {p}" for p in problems)
        else:
            extra = f", {', '.join(wrong_notes)}" if wrong_notes else ""
            report.lines.append(
                f"    {q.order_no} 고칠 줄: OK 고치기 전 {_describe(before)} → 고친 뒤 {after.output!r}{extra}"
            )
        if str(check["line"] if isinstance(check["line"], int) else check["line"][0]) not in correct:
            report.warnings.append(f"{where}: 정답 선택지에 고칠 줄 번호({check['line']})가 안 보임")


def check_course(course, run=True, strict=False):
    report = Report()
    report.lines.append(f"{course.slug} - {course.title} (레슨 {len(course.lessons)}개)")
    if course.unknown_keys:
        report.errors.append(f"{course.slug}/course.toml: 모르는 항목 {course.unknown_keys}")
    if not course.title:
        report.errors.append(f"{course.slug}/course.toml: title이 비어 있음")

    numbers = [l.order_no for l in course.lessons]
    if numbers != list(range(1, len(numbers) + 1)):
        report.errors.append(f"{course.slug}: 레슨 번호가 1부터 빈칸 없이 이어지지 않음 {numbers}")

    answers = []
    for lesson in course.lessons:
        where = f"{course.slug}/{lesson.path.name}"
        expected_name = f"L{lesson.order_no:02d}.toml" if isinstance(lesson.order_no, int) else "?"
        if lesson.kind == "lesson" and lesson.path.name != expected_name:
            report.errors.append(f"{where}: 파일 이름은 {expected_name}이어야 함 (order_no = {lesson.order_no})")
        if lesson.unknown_keys:
            report.errors.append(f"{where}: 모르는 항목 {lesson.unknown_keys} (오타이거나 [check]를 문제 밖에 둔 경우)")
        if lesson.kind not in KINDS:
            report.errors.append(f"{where}: kind '{lesson.kind}'은 없음")
        if lesson.track not in TRACKS:
            report.errors.append(f"{where}: 트랙 '{lesson.track}'은 없음 ({', '.join(TRACKS)} 중 하나)")
        if not lesson.title:
            report.errors.append(f"{where}: 제목(title)이 비어 있음")
        if not lesson.body_md:
            report.errors.append(f"{where}: 본문(body)이 비어 있음")
        if not isinstance(lesson.est_minutes, int) or not 3 <= lesson.est_minutes <= 15:
            report.errors.append(f"{where}: est_minutes {lesson.est_minutes!r}는 3~15분이어야 함")
        if not isinstance(lesson.is_published, bool):
            report.errors.append(f"{where}: published는 true 또는 false")
        count = len(lesson.questions)
        if lesson.kind == "lesson" and not 2 <= count <= 3:
            report.errors.append(f"{where}: 문제는 2~3개여야 함 (지금 {count}개)")

        state = "공개" if lesson.is_published else "초안"
        report.lines.append(f"  L{lesson.order_no:02d} [{TRACKS.get(lesson.track, '?')}·{state}] {lesson.title}"
                            if isinstance(lesson.order_no, int) else f"  {lesson.path.name}")
        for q in lesson.questions:
            _check_question(f"{where} 문제{q.order_no}", q, report, run)
            if isinstance(q.answer, int):
                answers.append(q.answer)

    # 정답 번호가 한쪽에 몰리면 찍어서 맞히기 쉬워진다
    if len(answers) >= 8:
        for n in range(1, 5):
            share = answers.count(n) / len(answers)
            if share > 0.4:
                report.warnings.append(f"{course.slug}: 정답이 {n}번에 {share:.0%} 몰림 ({len(answers)}문제 중)")

    if strict and report.skipped:
        report.errors.extend(f"{s} (strict 모드라 실패)" for s in report.skipped)
    return report


def check_all(root=LESSONS_DIR, run=True, strict=False):
    """모든 과정을 읽고 검사한다. 파일을 못 읽으면 그 내용이 오류로 들어간다."""
    combined = Report()
    try:
        courses = load_all(root)
    except (LessonFileError, OSError) as e:
        combined.errors.append(str(e))
        return [], combined
    if not courses:
        combined.errors.append(f"{root}에 과정(course.toml)이 없음")
    for course in courses:
        r = check_course(course, run=run, strict=strict)
        for name in ("errors", "warnings", "skipped", "lines"):
            getattr(combined, name).extend(getattr(r, name))
    return courses, combined


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="레슨 파일을 검사한다")
    parser.add_argument("--dir", default=str(LESSONS_DIR), help="레슨 폴더 (기본: 저장소의 lessons)")
    parser.add_argument("--no-run", action="store_true", help="코드를 실행하지 않고 형식만 본다")
    parser.add_argument("--strict", action="store_true", help="건너뛴 문제도 실패로 본다")
    args = parser.parse_args(argv)

    _, report = check_all(args.dir, run=not args.no_run, strict=args.strict)
    print("\n".join(report.lines))
    for w in report.warnings:
        print(f"경고: {w}")
    for e in report.errors:
        print(f"오류: {e}")
    print(f"\n결과: 오류 {len(report.errors)}개, 경고 {len(report.warnings)}개, 건너뜀 {len(report.skipped)}개")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
