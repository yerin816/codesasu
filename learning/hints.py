"""사수 힌트 만들기 (FR-08, NFR-04, WBS 4.2)

순서: 레슨 청크 검색 → 모델에 단계별 지시와 함께 요청 → 정답 노출 검사
     → 걸리면 한 번 다시 만들기 → 또 걸리면 고정 문구(blocked)
모델에는 힌트 품질을 위해 정답을 알려 주되, 답변에 정답이 새면 서버가 막는다.
OpenAI로는 문제·레슨 내용만 보내고 사용자 정보(이메일·이름)는 보내지 않는다 (NFR-05).
"""

import math
import re

from django.db import connection
from django.db.models import Q

from .chunks import text_for_embedding
from .embeddings import embed_texts
from .llm import LLMError, chat_json, hint_model
from .models import Lesson, LessonChunk

DAILY_LIMIT = 20  # 사용자당 하루 힌트 수 (NFR-03, 가설)
TOP_K = 3         # 근거로 쓸 레슨 조각 수

LEVELS = {
    # 단계마다 '말해도 되는 것'과 '말하면 안 되는 것'을 나눠 둔다.
    # 10/7 실제 점검: 원인·고칠 곳·결론을 다른 말로 풀어 정답이 드러나는 경우가 있어 금지 항목을 명시했다.
    1: ("방향", "무엇을 확인해 보면 좋을지 '관점'만 질문 하나로 던져요. 원인, 문제가 있는 곳, 결론, 나올 값은 말하지 않아요."),
    2: ("위치", "살펴볼 줄이나 부분만 짚고, 그 줄이 원래 하는 일을 설명해요. 그 줄에 무엇이 문제인지, 어떻게 고쳐야 하는지, 실행하면 무엇이 나오는지는 말하지 않아요."),
    3: ("개념", "필요한 개념 하나를 레슨 근거로 일반적인 말로 설명해요. 그 개념을 이 문제에 적용한 결론(무엇이 원인인지, 어느 선택지가 맞는지, 작업 순서가 어떻게 되는지)은 말하지 않고 후배가 직접 내리게 남겨 둬요."),
}

FALLBACK = {
    1: "문제에서 무엇을 묻는지 한 번 더 읽어 볼까요? 코드에서 그 질문과 관련된 부분이 어디인지 먼저 찾아보세요.",
    2: "코드를 위에서부터 한 줄씩 따라가면서, 값이 처음 정해지거나 바뀌는 줄에 표시해 보세요.",
    3: "이 레슨 본문에 실마리가 있어요. 본문을 다시 읽고, 선택지를 하나씩 코드에 넣어 보며 결과를 비교해 보세요.",
}

SYSTEM_PROMPT = """당신은 학습 앱 '코드사수'의 사수입니다. 개발자가 아닌 직장인 후배가 AI가 짠 파이썬 업무 코드를 읽다가 막혔을 때 힌트를 주는 친절한 3년 차 선배예요.
말투: 존댓말(해요체), 다정하고 짧게. 혼내거나 재촉하지 않아요.

반드시 지킬 것:
1. 정답 선택지의 번호와 문구, 코드의 실행 결과 값, 고친 코드를 절대 말하지 않아요. '정답은'이라는 표현도 쓰지 않아요.
2. 정답을 다른 말로 풀어서 말하는 것도 정답을 말한 거예요. 후배가 힌트만 읽고 바로 답을 고를 수 있다면 너무 많이 말한 것이니, 한 단계에 한 걸음만 도와요.
3. 근거는 '레슨 발췌'와 '질문' 문장에 적힌 설명만 써요. 둘 다에 없는 내용은 추측하지 말고 '레슨에서 아직 다루지 않은 부분'이라고 말해요.
4. 2~3문장, 200자 안쪽. 여러 줄짜리 코드 블록은 쓰지 않아요. 짧은 코드는 `if`처럼 한 줄 안에서만 써요.
5. 앞에서 준 힌트와 같은 말을 반복하지 않아요.
6. used_excerpts에는 실제로 근거로 삼은 레슨 발췌 번호만 넣어요."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "message": {"type": "string"},
        "used_excerpts": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["message", "used_excerpts"],
    "additionalProperties": False,
}

CIRCLED = "①②③④⑤⑥⑦⑧⑨"


# ---------- 근거 찾기 ----------

_question_vectors = {}  # 같은 문제를 다시 임베딩하지 않게 서버 메모리에 잠깐 기억


def question_vector(question):
    key = (question.id, hash(question.prompt + question.code))
    if key not in _question_vectors:
        text = text_for_embedding(question.lesson.title, f"{question.prompt}\n{question.code}")
        vectors, _ = embed_texts([text])
        _question_vectors[key] = vectors[0]
    return _question_vectors[key]


def _cosine_distance(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return 1 - dot / (na * nb) if na and nb else 1.0


def find_sources(question, k=TOP_K):
    """같은 과정의 공개 레슨(과 이 문제의 레슨) 조각 중 문제와 가장 가까운 k개.
    PostgreSQL(Supabase)은 pgvector로 DB 안에서 찾고, 테스트용 SQLite는 파이썬으로 계산한다."""
    vector = question_vector(question)
    # 공개 레슨 + 이 문제의 레슨 자신 (관리자가 초안을 미리 풀 때도 자기 본문을 근거로 쓰게)
    chunks = LessonChunk.objects.filter(
        Q(lesson__is_published=True) | Q(lesson_id=question.lesson_id),
        lesson__course_id=question.lesson.course_id,
        lesson__kind=Lesson.Kind.LESSON,
    ).select_related("lesson")
    if connection.vendor == "postgresql":
        from pgvector.django import CosineDistance

        return list(chunks.annotate(distance=CosineDistance("embedding", vector)).order_by("distance")[:k])
    ranked = sorted(chunks, key=lambda c: _cosine_distance(list(c.embedding), vector))
    return ranked[:k]


# ---------- 정답 노출 검사 ----------

def _normalize(text):
    return re.sub(r"\s+", "", str(text)).lower()


def find_leaks(message, question):
    """힌트 문장에서 정답이 드러나는 곳을 찾는다. 빈 목록이면 통과."""
    leaks = []
    n = question.answer
    answer_text = str(question.choices[n - 1]) if 1 <= n <= len(question.choices) else ""

    if re.search(r"정답은|정답이에요|정답입니다|답은\s", message):
        leaks.append("'정답은' 표현")
    # "2번", "②" (단, "2번째 줄"처럼 줄 번호는 허용)
    if re.search(rf"(?<![\d.]){n}\s*번(?!\s*째|\s*줄|\s*행)", message) or (n <= len(CIRCLED) and CIRCLED[n - 1] in message):
        leaks.append("정답 번호")

    norm_msg, norm_ans = _normalize(message), _normalize(answer_text)
    if len(norm_ans) >= 4:
        if norm_ans in norm_msg:
            leaks.append("정답 선택지 문구")
        else:
            # 긴 선택지는 "→" 등으로 나눈 한 토막(8자 이상)만 그대로 나와도 노출로 본다
            for part in re.split(r"[→,.]", answer_text):
                if len(_normalize(part)) >= 8 and _normalize(part) in norm_msg:
                    leaks.append("정답 선택지 일부")
                    break
    elif answer_text:
        # "1", "0"처럼 짧은 정답은 '출력/결과/답' 근처에 나올 때만 노출로 본다
        # 숫자 경계는 숫자·영문자만 본다 ("1이에요"의 '이'는 한글 조사라 경계로 쳐야 한다)
        a = re.escape(answer_text.strip())
        alone = rf"(?<![0-9A-Za-z.]){a}(?![0-9A-Za-z.])"
        if re.search(rf"(출력|결과|답|값)[^.?!\n]{{0,12}}{alone}", message) or \
                re.search(rf"{alone}\s*(이|가)?\s*(출력|나와|나옵|나올|찍혀|찍힙|돼요|됩니다)", message):
            leaks.append("실행 결과 값")

    code_lines = [line for line in message.splitlines() if re.search(r"[=():\[\]]", line)]
    if "```" in message or len(code_lines) >= 3:
        leaks.append("코드 블록")
    return leaks


# ---------- 힌트 만들기 ----------

def _user_prompt(question, level, chunks, previous, retry_reason):
    lines = [
        f"[이번 단계] {level}단계 - {LEVELS[level][0]}: {LEVELS[level][1]}",
        "",
        f"[문제 유형] {question.get_qtype_display()}",
        f"[질문] {question.prompt}",
        "[코드]",
        question.code or "(없음)",
        "[선택지]",
        *[f"{i}. {c}" for i, c in enumerate(question.choices, 1)],
        f"[정답 번호 - 참고만 하고 절대 말하지 마세요] {question.answer}",
        "",
        "[레슨 발췌]",
        *[f"({i}) 레슨 '{c.lesson.title}': {c.content}" for i, c in enumerate(chunks, 1)],
    ]
    if previous:
        lines += ["", "[앞에서 준 힌트]", *[f"- {p}" for p in previous]]
    if retry_reason:
        lines += ["", f"[다시 쓰기] 직전 답변이 정답을 드러냈어요({retry_reason}). 정답 번호·문구·결과 값을 빼고 다시 써 주세요."]
    return "\n".join(lines)


def _sources(chunks, used):
    picked = [chunks[i - 1] for i in used if isinstance(i, int) and 1 <= i <= len(chunks)] or chunks[:1]
    seen, sources = set(), []
    for c in picked:
        if c.lesson_id not in seen:
            seen.add(c.lesson_id)
            sources.append({"lesson_id": c.lesson_id, "title": c.lesson.title})
    return sources


def generate_hint(question, level, previous=()):
    """힌트 하나를 만든다. 반환: message, sources, blocked, attempts, model, tokens_in, tokens_out.
    OpenAI 호출이 실패하면 LLMError(또는 EmbeddingError)를 그대로 올린다."""
    chunks = find_sources(question)
    total = {"tokens_in": 0, "tokens_out": 0}
    retry_reason = ""
    for attempt in (1, 2):
        data, usage = chat_json(
            SYSTEM_PROMPT,
            _user_prompt(question, level, chunks, list(previous), retry_reason),
            "sasu_hint",
            OUTPUT_SCHEMA,
        )
        total["tokens_in"] += usage["tokens_in"]
        total["tokens_out"] += usage["tokens_out"]
        message = (data.get("message") or "").strip()
        leaks = find_leaks(message, question) if message else ["빈 답변"]
        if not leaks:
            return {
                "message": message, "sources": _sources(chunks, data.get("used_excerpts", [])),
                "blocked": False, "attempts": attempt, "leaks": [], "model": hint_model(), **total,
            }
        retry_reason = ", ".join(leaks)

    # 두 번 다 걸리면 정답이 섞일 위험이 없는 고정 문구를 보낸다
    return {
        "message": FALLBACK[level],
        "sources": [{"lesson_id": question.lesson_id, "title": question.lesson.title}],
        "blocked": True, "attempts": 2, "leaks": retry_reason.split(", "), "model": hint_model(), **total,
    }


__all__ = ["DAILY_LIMIT", "LEVELS", "LLMError", "find_leaks", "find_sources", "generate_hint"]
