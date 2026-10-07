"""OpenAI 답변 모델 호출 (Chat Completions + 구조화 출력)

embeddings.py와 같은 이유로 openai 라이브러리 대신 requests로 직접 부른다.
모델 이름은 .env의 HINT_MODEL로 바꿀 수 있다 (기본 gpt-6-luna, 품질이 부족하면 gpt-6.1-sol).
"""

import json
import os

import requests

from .embeddings import hide_keys

API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-6-luna"
REASONING_EFFORT = "low"      # 힌트는 짧은 답이라 생각 단계를 낮춰 비용·시간을 아낀다
MAX_COMPLETION_TOKENS = 4000  # 생각(reasoning) 토큰까지 포함한 상한. 넉넉히 둬야 답이 잘리지 않는다


class LLMError(Exception):
    pass


def hint_model():
    return os.environ.get("HINT_MODEL") or DEFAULT_MODEL


def chat_json(system, user, schema_name, schema):
    """system·user 메시지를 보내고, schema 모양의 JSON(dict)과 토큰 사용량을 돌려준다."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise LLMError(".env에 OPENAI_API_KEY가 없어요.")

    payload = {
        "model": hint_model(),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "reasoning_effort": REASONING_EFFORT,
        "max_completion_tokens": MAX_COMPLETION_TOKENS,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": schema, "strict": True},
        },
    }
    try:
        res = requests.post(
            API_URL, headers={"Authorization": f"Bearer {api_key}"}, json=payload, timeout=60
        )
    except requests.RequestException as e:
        raise LLMError(f"OpenAI에 연결하지 못했어요: {e.__class__.__name__}") from None

    if res.status_code != 200:
        try:
            message = res.json().get("error", {}).get("message", "")
        except ValueError:
            message = res.text[:200]
        raise LLMError(f"OpenAI 응답 {res.status_code}: {hide_keys(message)}")

    body = res.json()
    choice = body["choices"][0]
    msg = choice.get("message", {})
    if msg.get("refusal"):
        raise LLMError("모델이 답변을 거절했어요.")
    content = msg.get("content") or ""
    if not content:
        # 생각 단계에서 토큰을 다 써서 답이 비는 경우 (finish_reason == "length")
        raise LLMError(f"빈 답변이 왔어요 (finish_reason={choice.get('finish_reason')}).")
    try:
        data = json.loads(content)
    except ValueError:
        raise LLMError("답변이 JSON 형식이 아니에요.") from None

    usage = body.get("usage", {})
    return data, {
        "tokens_in": usage.get("prompt_tokens", 0),
        "tokens_out": usage.get("completion_tokens", 0),  # 생각 토큰 포함 (출력 요금으로 계산됨)
        "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0),
    }
