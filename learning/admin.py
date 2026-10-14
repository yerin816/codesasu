from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from .models import AnswerAttempt, Course, ExplanationReveal, HintRequest, Lesson, LessonProgress, Profile, Question


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
    # 폰에서 표가 옆으로 길어 미리보기 링크가 안 보여서 번호 바로 옆에 둔다 (10/12 폰 점검)
    list_display = ["order_no", "app_link", "is_published", "title", "track", "est_minutes"]
    list_filter = ["course", "track", "is_published"]
    inlines = [QuestionInline]
    readonly_fields = ["course", "order_no", "kind", "track", "title", "body_md", "est_minutes", "is_published"]

    def has_add_permission(self, request):
        return False

    @admin.display(description="앱 화면")
    def app_link(self, obj):
        # 관리자 계정이면 공개 전 초안도 실제 레슨 화면으로 열린다 (폰 점검용)
        label = "미리보기" if not obj.is_published else "열기"
        return format_html('<a href="{}" target="_blank" rel="noopener">{}</a>', reverse("lesson", args=[obj.id]), label)

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


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    """주간 목표 분포를 보는 용도 (베타 분석). 값은 사용자가 화면에서 바꾼다"""

    list_display = ["user", "weekly_goal_days", "onboarded_at", "updated_at"]
    list_filter = ["weekly_goal_days"]
    readonly_fields = ["user", "weekly_goal_days", "notify_time", "timezone", "onboarded_at", "updated_at"]

    def has_add_permission(self, request):
        return False
