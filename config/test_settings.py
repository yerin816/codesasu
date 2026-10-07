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
