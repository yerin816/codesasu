from django.urls import path

from . import views

urlpatterns = [
    path("today", views.today, name="api-today"),
    path("lessons/<int:lesson_id>", views.lesson_detail, name="api-lesson-detail"),
    path("lessons/<int:lesson_id>/progress", views.start_progress, name="api-start-progress"),
    path("progress/<int:progress_id>/answers", views.submit_answer, name="api-submit-answer"),
    path("progress/<int:progress_id>/explanation", views.reveal_explanation, name="api-reveal-explanation"),
    path("progress/<int:progress_id>/complete", views.complete_progress, name="api-complete-progress"),
    path("questions/<int:question_id>/hints", views.request_hint, name="api-request-hint"),
    path("me", views.me, name="api-me"),
    path("progress/weekly", views.weekly_progress, name="api-weekly-progress"),
    path("progress/lessons", views.lesson_records, name="api-lesson-records"),
]
