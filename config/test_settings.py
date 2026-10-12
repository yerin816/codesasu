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

# 테스트는 일부러 잘못된 요청을 많이 보내므로, 400·404 경고 로그는 화면에 찍지 않는다
LOGGING = {**LOGGING, "loggers": {"django": {"handlers": ["console"], "level": "ERROR", "propagate": False}}}  # noqa: F405

# 테스트 실행기는 DEBUG를 False로 바꿔서 돌리므로, whitenoise가 staticfiles 폴더(배포 때만 만듦)를 찾다가 경고를 낸다.
# 테스트에는 영향이 없는 경고라 끈다.
import warnings  # noqa: E402

warnings.filterwarnings("ignore", message="No directory at")
