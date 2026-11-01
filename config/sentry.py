"""서버 오류 알림 (WBS 6.1): Sentry

서버에서 오류(500)가 나면 Sentry가 어느 코드 줄에서 났는지 모아서 메일로 알려 준다.
환경 변수 SENTRY_DSN이 있을 때만 켜진다 (PC 개발·테스트에서는 꺼짐).

사용자 정보는 보내지 않는다 (개인정보처리방침 4번).
- 이메일·IP·쿠키: send_default_pii=False
- 요청 본문(고른 답, 비밀번호 칸 등)과 오류 난 줄의 변수 값: 보내지 않음
- 요청 헤더: 브라우저 종류(User-Agent)만 남기고 지운다 (X-Cron-Token 같은 비밀 헤더가 새지 않게)
- 주소 속 비밀값: 비밀번호 재설정·이메일 확인 링크의 키, 알림 받는 주소(푸시 서비스 URL)는 가린다
"""

import re

KEEP_HEADERS = {"user-agent"}
MASK = "[가림]"

# 메일 링크 속 일회용 키. /accounts/password/reset/key/<키>/ , /accounts/confirm-email/<키>/
_SECRET_PATH = re.compile(r"(/accounts/(?:password/reset/key|confirm-email)/)[^/?#]+")
# 바깥으로 보낸 요청(푸시 서비스·OpenAI)은 주소의 경로를 지우고 서버 이름만 남긴다
_URL_PATH = re.compile(r"^(https?://[^/?#]+)[^\s]*$")


def mask_path(text):
    return _SECRET_PATH.sub(rf"\1{MASK}", text) if isinstance(text, str) else text


def before_send(event, hint):
    request = event.get("request")
    if request:
        if "url" in request:
            request["url"] = mask_path(request["url"])
        headers = request.get("headers") or {}
        request["headers"] = {k: v for k, v in headers.items() if k.lower() in KEEP_HEADERS}
        for key in ("data", "cookies", "env"):
            request.pop(key, None)
    if "transaction" in event:
        event["transaction"] = mask_path(event["transaction"])
    event.pop("user", None)
    return event


def before_breadcrumb(crumb, hint):
    data = crumb.get("data") or {}
    url = data.get("url")
    if isinstance(url, str):
        data["url"] = _URL_PATH.sub(r"\1/…", url)
        for key in ("http.query", "http.fragment"):
            data.pop(key, None)
    if isinstance(crumb.get("message"), str):
        crumb["message"] = mask_path(crumb["message"])
    return crumb


def init_sentry(dsn, *, environment, release=None, transport=None):
    import sentry_sdk

    options = dict(
        dsn=dsn,
        environment=environment,
        release=release,
        send_default_pii=False,
        max_request_body_size="never",
        include_local_variables=False,
        traces_sample_rate=0,  # 성능 측정은 끄고 오류만 받는다 (무료 한도 아끼기)
        before_send=before_send,
        before_breadcrumb=before_breadcrumb,
    )
    if transport is not None:  # 시험에서만: 밖으로 보내지 않고 잡아 둔다
        options["transport"] = transport
    sentry_sdk.init(**options)
