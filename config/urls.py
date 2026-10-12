from allauth.account.decorators import secure_admin_login
from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from learning import pages

# 관리자 로그인도 일반 로그인 화면(allauth)을 거치게 한다.
# 그쪽에 로그인 시도 제한이 있고, 구글 계정(2단계 인증)으로 들어올 수 있다.
# 로그인한 사람이 관리자(is_staff)가 아니면 403
admin.site.login = secure_admin_login(admin.site.login)

urlpatterns = [
    path(settings.ADMIN_PATH, admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("api/v1/", include("learning.urls")),
    path("manifest.webmanifest", pages.manifest, name="manifest"),
    path("sw.js", pages.service_worker, name="service-worker"),
    path("lessons/<int:lesson_id>/", pages.lesson, name="lesson"),
    path("", pages.home, name="home"),
]
