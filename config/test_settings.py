"""테스트 전용 설정. 실제 Supabase DB 대신 메모리 SQLite를 써서 테스트가 운영 데이터를 건드리지 않게 한다.

실행: python manage.py test learning --settings=config.test_settings
"""

from .settings import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# 배포용 보안 설정(https로 보내기, https 전용 쿠키)은 테스트에서 끈다.
# 테스트는 http로 요청하므로, 켜져 있으면 모든 요청이 https로 넘어가 버린다.
# 결과가 .env의 DEBUG 값에 따라 달라지지 않게 여기서 고정한다.
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
