"""웹 푸시 보내기 (WBS 5.2, FR-14)

표준 두 가지를 직접 구현한다. 흔히 쓰는 pywebpush는 의존 패키지(http-ece)가 설치되지 않아서,
이미 쓰고 있는 cryptography·PyJWT·requests만으로 만든다.
- RFC 8291: 알림 내용을 그 브라우저만 풀 수 있게 암호화 (aes128gcm)
- RFC 8292 (VAPID): 우리 서버가 보낸 알림임을 푸시 서비스에 증명하는 서명

설정(환경 변수)
- VAPID_PRIVATE_KEY: 서버 서명용 개인 키 (base64url, 32바이트). 없으면 알림 기능이 꺼진다
- VAPID_SUBJECT: 푸시 서비스가 문제 있을 때 연락할 주소 (mailto:...)
"""

import base64
import json
import os
import secrets
import time
from urllib.parse import urlsplit

import jwt
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

RECORD_SIZE = 4096
TTL_SECONDS = 6 * 60 * 60  # 6시간 안에 폰이 켜지지 않으면 그 알림은 버린다 (하루 지난 '오늘의 레슨' 알림은 의미 없음)

# 구독 주소는 사용자가 보내는 값이라, 아무 주소나 받으면 서버가 남의 서버로 요청을 보내게 된다(SSRF).
# 실제 브라우저 푸시 서비스 주소만 받는다
ALLOWED_PUSH_HOSTS = (
    "fcm.googleapis.com",          # 크롬·안드로이드·엣지 일부
    "push.services.mozilla.com",   # 파이어폭스
    "push.apple.com",              # 아이폰·맥 사파리 (web.push.apple.com)
    "notify.windows.com",          # 엣지(윈도우)
)


class PushError(Exception):
    pass


def b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def is_allowed_endpoint(endpoint: str) -> bool:
    try:
        parts = urlsplit(endpoint)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or parts.port not in (None, 443):
        return False
    return any(host == h or host.endswith("." + h) for h in ALLOWED_PUSH_HOSTS)


# ---------- 키 ----------

def private_key_from_b64(value: str) -> ec.EllipticCurvePrivateKey:
    raw = b64u_decode(value)
    if len(raw) != 32:
        raise PushError("VAPID 개인 키는 32바이트여야 해요")
    return ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())


def public_key_bytes(private_key) -> bytes:
    return private_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def generate_vapid_keys():
    """(개인 키, 공개 키) base64url 문자열. 처음 한 번 만들어 환경 변수에 넣는다"""
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_numbers().private_value.to_bytes(32, "big")
    return b64u_encode(private), b64u_encode(public_key_bytes(key))


def vapid_private_key():
    value = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    return private_key_from_b64(value) if value else None


def vapid_public_key_b64():
    """브라우저가 구독할 때 쓰는 서버 공개 키. 설정이 없으면 None (알림 꺼짐)"""
    key = vapid_private_key()
    return b64u_encode(public_key_bytes(key)) if key else None


# ---------- RFC 8291 암호화 ----------

def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(plaintext: bytes, ua_public: bytes, auth_secret: bytes, *, as_private=None, salt: bytes | None = None) -> bytes:
    """브라우저 공개 키(ua_public)와 auth 비밀값으로 암호화한 본문(aes128gcm)을 돌려준다.
    as_private·salt는 시험(RFC 예제)에서만 넣고, 실제로는 보낼 때마다 새로 만든다"""
    as_private = as_private or ec.generate_private_key(ec.SECP256R1())
    salt = salt or secrets.token_bytes(16)
    as_public = public_key_bytes(as_private)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    shared = as_private.exchange(ec.ECDH(), ua_key)

    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)

    if len(plaintext) + 1 + 16 > RECORD_SIZE:
        raise PushError("알림 내용이 너무 길어요")
    ciphertext = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)  # \x02 = 마지막 레코드 표시
    header = salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(as_public)]) + as_public
    return header + ciphertext


# ---------- RFC 8292 VAPID ----------

def vapid_authorization(endpoint: str, private_key, subject: str) -> str:
    parts = urlsplit(endpoint)
    claims = {"aud": f"{parts.scheme}://{parts.netloc}", "exp": int(time.time()) + 12 * 60 * 60, "sub": subject}
    token = jwt.encode(claims, private_key, algorithm="ES256")
    return f"vapid t={token}, k={b64u_encode(public_key_bytes(private_key))}"


# ---------- 보내기 ----------

GONE = "gone"  # 구독이 사라짐 (앱 삭제·알림 끔) → DB에서 지운다


def send(subscription, payload: dict, *, timeout: float = 10.0) -> str:
    """구독 하나에 알림을 보낸다. 결과: "ok" / GONE / "error:<상태>" """
    key = vapid_private_key()
    if key is None:
        raise PushError("VAPID_PRIVATE_KEY가 설정되지 않았어요")
    if not is_allowed_endpoint(subscription.endpoint):
        return GONE
    body = encrypt(
        json.dumps(payload, ensure_ascii=False).encode(),
        b64u_decode(subscription.p256dh), b64u_decode(subscription.auth),
    )
    subject = os.environ.get("VAPID_SUBJECT", "mailto:ningyerin@gmail.com")
    try:
        res = requests.post(subscription.endpoint, data=body, timeout=timeout, headers={
            "Authorization": vapid_authorization(subscription.endpoint, key, subject),
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "TTL": str(TTL_SECONDS),
            "Urgency": "normal",
        })
    except requests.RequestException as e:
        return f"error:{type(e).__name__}"
    if res.status_code in (200, 201, 202):
        return "ok"
    if res.status_code in (404, 410):
        return GONE
    return f"error:{res.status_code}"
