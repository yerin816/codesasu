"""레슨 본문을 힌트 검색용 조각(청크)으로 자른다 (WBS 4.1)

규칙은 9/29 파이프라인 실험에서 고른 값을 그대로 쓴다.
- 문장 단위로 자르고, 한 조각은 최대 300자
- 앞 조각의 마지막 문장 1개를 다음 조각 앞에 겹쳐서 문맥이 끊기지 않게 한다
- 문단(빈 줄)이 바뀌면 새 조각을 시작한다
"""

import hashlib
import re

MAX_CHARS = 300

# 마침표·물음표·느낌표 뒤 공백에서 문장을 나눈다 ("돼요. 그" → "돼요." | "그")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def source_hash(title, body):
    """레슨 제목+본문의 지문. 내용이 같으면 다시 임베딩하지 않는다."""
    return hashlib.sha256(f"{title}\n\n{body}".encode("utf-8")).hexdigest()


def split_sentences(paragraph):
    return [s.strip() for s in _SENTENCE_END.split(paragraph.strip()) if s.strip()]


def split_into_chunks(body, max_chars=MAX_CHARS):
    """본문을 조각 목록(문자열)으로 나눈다."""
    chunks = []
    for paragraph in re.split(r"\n\s*\n", body or ""):
        sentences = split_sentences(paragraph)
        current = []
        for sentence in sentences:
            candidate = " ".join(current + [sentence])
            if current and len(candidate) > max_chars:
                chunks.append(" ".join(current))
                # 바로 앞 문장 하나를 겹쳐서 다음 조각을 시작한다
                current = [current[-1], sentence] if len(current[-1]) + len(sentence) + 1 <= max_chars else [sentence]
            else:
                current.append(sentence)
        if current:
            chunks.append(" ".join(current))
    return chunks


def text_for_embedding(lesson_title, chunk):
    """임베딩할 때는 레슨 제목을 앞에 붙여, 짧은 조각도 어느 레슨 이야기인지 알 수 있게 한다."""
    return f"레슨: {lesson_title}\n{chunk}"
