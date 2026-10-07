"""OpenAI 임베딩 호출 (text-embedding-3-small, 1536차원)

openai 라이브러리 대신 이미 설치된 requests로 API를 직접 부른다.
openai 라이브러리는 pydantic(DLL 포함)을 함께 설치하는데, 이 PC에서는 psycopg처럼
애플리케이션 제어 정책에 막힐 수 있어서다. 키는 .env의 OPENAI_API_KEY에서만 읽고 출력하지 않는다.
"""

import os
import re

import requests

from .models import EMBEDDING_DIMENSIONS

EMBEDDING_MODEL = "text-embedding-3-small"
API_URL = "https://api.openai.com/v1/embeddings"
BATCH_SIZE = 100  # 한 번에 보내는 조각 수


class EmbeddingError(Exception):
    pass


def hide_keys(text):
    """OpenAI 에러 문구에는 가린 키 일부(sk-proj-****Ec0A)가 섞여 온다. 화면·로그에 남지 않게 통째로 가린다."""
    return re.sub(r"sk-[A-Za-z0-9_\-*]+", "sk-…(가림)", text)


def embed_texts(texts):
    """문자열 목록 → (임베딩 목록, 사용한 토큰 수). 순서는 입력과 같다."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise EmbeddingError(".env에 OPENAI_API_KEY가 없어요. 키를 넣은 뒤 다시 실행해 주세요.")

    vectors, total_tokens = [], 0
    for start in range(0, len(texts), BATCH_SIZE):
        batch = texts[start:start + BATCH_SIZE]
        try:
            res = requests.post(
                API_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": EMBEDDING_MODEL, "input": batch},
                timeout=60,
            )
        except requests.RequestException as e:
            raise EmbeddingError(f"OpenAI에 연결하지 못했어요: {e.__class__.__name__}") from None
        if res.status_code != 200:
            # 원인 파악을 위해 OpenAI 에러 문구를 보여 주되, 섞여 오는 키 조각은 hide_keys로 가린다
            try:
                message = res.json().get("error", {}).get("message", "")
            except ValueError:
                message = res.text[:200]
            raise EmbeddingError(f"OpenAI 응답 {res.status_code}: {hide_keys(message)}")

        body = res.json()
        data = sorted(body["data"], key=lambda d: d["index"])
        for item in data:
            if len(item["embedding"]) != EMBEDDING_DIMENSIONS:
                raise EmbeddingError(f"임베딩 차원이 {len(item['embedding'])}이에요 (기대값 {EMBEDDING_DIMENSIONS}).")
            vectors.append(item["embedding"])
        total_tokens += body.get("usage", {}).get("total_tokens", 0)
    return vectors, total_tokens
