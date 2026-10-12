from django.contrib import admin

from .models import AnswerAttempt, Course, ExplanationReveal, HintRequest, Lesson, LessonProgress, Question


# 과정·레슨·문제 내용은 lessons/ 폴더의 파일이 원본이다 (sync_lessons가 서버 시작 때 반영).
# 여기서 고쳐도 다음 배포 때 파일 내용으로 덮이므로 읽기 전용으로 둔다.
CONTENT_NOTE = "내용 수정은 lessons/ 폴더의 레슨 파일에서 하세요 (이 화면은 읽기 전용)"


class QuestionInline(admin.StackedInline):
    model = Question
    extra = 0
    readonly_fields = ["order_no", "qtype", "prompt", "code", "choices", "answer", "explanation"]

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ["title", "slug", "is_published"]
    readonly_fields = ["slug", "title", "language", "is_published"]

    def has_add_permission(self, request):
        return False


@admin.register(Lesson)
class LessonAdmin(admin.ModelAdmin):
    list_display = ["order_no", "title", "track", "est_minutes", "is_published"]
    list_filter = ["course", "track", "is_published"]
    inlines = [QuestionInline]
    readonly_fields = ["course", "order_no", "kind", "track", "title", "body_md", "est_minutes", "is_published"]

    def has_add_permission(self, request):
        return False

    def changelist_view(self, request, extra_context=None):
        from django.contrib import messages
        messages.info(request, CONTENT_NOTE)
        return super().changelist_view(request, extra_context)


@admin.register(LessonProgress)
class LessonProgressAdmin(admin.ModelAdmin):
    list_display = ["lesson", "user", "guest_key", "started_at", "completed_at", "correct_count"]


@admin.register(AnswerAttempt)
class AnswerAttemptAdmin(admin.ModelAdmin):
    list_display = ["progress", "question", "selected", "is_correct", "created_at"]


@admin.register(ExplanationReveal)
class ExplanationRevealAdmin(admin.ModelAdmin):
    list_display = ["progress", "question", "created_at"]


@admin.register(HintRequest)
class HintRequestAdmin(admin.ModelAdmin):
    list_display = ["question", "level", "blocked", "model", "tokens_in", "tokens_out", "created_at"]
    list_filter = ["blocked", "level", "model"]
