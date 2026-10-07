from django.apps import AppConfig


class LearningConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'learning'

    def ready(self):
        from . import signals  # noqa: F401  로그인 시 게스트 기록 연결
