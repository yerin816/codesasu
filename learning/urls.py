from django.urls import path

from . import views

urlpatterns = [
    path("today", views.today, name="api-today"),
    path("lessons/<int:lesson_id>", views.lesson_detail, name="api-lesson-detail"),
    path("lessons/<int:lesson_id>/progress", views.start_progress, name="api-start-progress"),
    path("progress/<int:progress_id>/answers", views.submit_answer, name="api-submit-answer"),
    path("progress/<int:progress_id>/explanation", views.reveal_explanation, name="api-reveal-explanation"),
    path("progress/<int:progress_id>/complete", views.complete_progress, name="api-complete-progress"),
]
