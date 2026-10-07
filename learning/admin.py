from django.contrib import admin

from .models import AnswerAttempt, Course, Lesson, LessonProgress, Question


class QuestionInline(admin.StackedInline):
    model = Question
    extra = 0


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ["title", "slug", "is_published"]


@admin.register(Lesson)
class LessonAdmin(admin.ModelAdmin):
    list_display = ["order_no", "title", "track", "est_minutes", "is_published"]
    list_filter = ["course", "track", "is_published"]
    inlines = [QuestionInline]


@admin.register(LessonProgress)
class LessonProgressAdmin(admin.ModelAdmin):
    list_display = ["lesson", "user", "guest_key", "started_at", "completed_at", "correct_count"]


@admin.register(AnswerAttempt)
class AnswerAttemptAdmin(admin.ModelAdmin):
    list_display = ["progress", "question", "selected", "is_correct", "created_at"]