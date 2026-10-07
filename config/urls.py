from django.contrib import admin
from django.urls import include, path

from learning import pages

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("api/v1/", include("learning.urls")),
    path("manifest.webmanifest", pages.manifest, name="manifest"),
    path("sw.js", pages.service_worker, name="service-worker"),
    path("lessons/<int:lesson_id>/", pages.lesson, name="lesson"),
    path("", pages.home, name="home"),
]
