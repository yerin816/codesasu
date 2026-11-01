from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from pgvector.django import VectorField

from .lesson_files import CODE_MAX_WIDTH, display_width, long_code_lines  # noqa: F401

EMBEDDING_DIMENSIONS = 1536  # OpenAI text-embedding-3-small


class Course(models.Model):
    """과정. 예: 파이썬 업무 자동화 30일"""

    slug = models.SlugField(unique=True)
    title = models.CharField(max_length=100)
    language = models.CharField(max_length=30, default="python")
    is_published = models.BooleanField(default=False)

    def __str__(self):
        return self.title


class Lesson(models.Model):
    """레슨 하나 = 본문 + 문제 2~3개 (FR-05)"""

    class Kind(models.TextChoices):
        LESSON = "lesson", "일반 레슨"
        DIAGNOSTIC = "diagnostic", "진단"

    class Track(models.TextChoices):
        READING = "reading", "읽기"
        ERROR = "error", "에러"
        SHIPPING = "shipping", "내보내기"

    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="lessons")
    order_no = models.PositiveIntegerField()
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.LESSON)
    track = models.CharField(max_length=20, choices=Track.choices)
    title = models.CharField(max_length=200)
    body_md = models.TextField()
    est_minutes = models.PositiveSmallIntegerField(default=10)
    is_published = models.BooleanField(default=False)

    class Meta:
        ordering = ["course", "order_no"]
        constraints = [
            models.UniqueConstraint(fields=["course", "order_no"], name="unique_lesson_order"),
        ]

    def __str__(self):
        return f"{self.order_no}. {self.title}"


# 폭 규칙(44칸, 한글 2칸)은 레슨 파일 검사기와 같이 쓴다


class Question(models.Model):
    """문제. answer는 화면(API)으로 보내지 않는다"""

    class QType(models.TextChoices):
        PREDICT = "predict", "결과 예측"
        ERROR = "error", "에러 해석"
        FIX_LINE = "fix_line", "고칠 줄 고르기"
        FLOW = "flow", "흐름 설명"

    lesson = models.ForeignKey(Lesson, on_delete=models.CASCADE, related_name="questions")
    order_no = models.PositiveIntegerField()
    qtype = models.CharField(max_length=20, choices=QType.choices)
    prompt = models.TextField()
    code = models.TextField(blank=True)
    choices = models.JSONField(help_text='선택지 목록. 예: ["0", "1", "2", "3"]')
    answer = models.JSONField(help_text="정답 선택지 번호(1부터 시작). 예: 2")
    explanation = models.TextField()

    class Meta:
        ordering = ["lesson", "order_no"]
        constraints = [
            models.UniqueConstraint(fields=["lesson", "order_no"], name="unique_question_order"),
        ]

    def __str__(self):
        return f"{self.lesson.order_no}-{self.order_no} {self.get_qtype_display()}"

    def clean(self):
        # 관리자 화면에서 문제를 저장할 때 긴 줄을 막는다 (레슨 작성 규칙)
        lines = long_code_lines(self.code or "")
        if lines:
            raise ValidationError({"code": f"폰 화면에서 잘리는 줄이 있어요: {', '.join(map(str, lines))}번째 줄. "
                                           f"한 줄을 {CODE_MAX_WIDTH}칸(한글은 2칸) 안으로 나눠 주세요."})


class LessonProgress(models.Model):
    """레슨 풀이 1회. 다시 풀면 새 행. 로그인 전 첫 레슨은 guest_key로 묶는다 (FR-01)"""

    class Phase(models.TextChoices):
        PRE = "pre", "사전 진단"
        POST = "post", "사후 진단"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
        related_name="lesson_progresses",
    )
    guest_key = models.CharField(max_length=64, blank=True)
    lesson = models.ForeignKey(Lesson, on_delete=models.CASCADE, related_name="progresses")
    phase = models.CharField(max_length=10, choices=Phase.choices, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    duration_sec = models.PositiveIntegerField(null=True, blank=True)
    correct_count = models.PositiveSmallIntegerField(default=0)
    hint_rating = models.SmallIntegerField(null=True, blank=True)

    def __str__(self):
        who = self.user.email if self.user else f"guest:{self.guest_key[:6]}"
        return f"{who} - {self.lesson}"


class AnswerAttempt(models.Model):
    """답안 제출 1회. 연속 오답 수는 이 테이블에서 센다 (FR-09)"""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
        related_name="answer_attempts",
    )
    progress = models.ForeignKey(LessonProgress, on_delete=models.CASCADE, related_name="attempts")
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="attempts")
    selected = models.JSONField()
    is_correct = models.BooleanField()
    created_at = models.DateTimeField(auto_now_add=True)

class ExplanationReveal(models.Model):
    """틀린 뒤 '해설 보기'를 누른 기록 (10/7 결정 A).
    해설을 본 문제는 다시 풀 수 없고, '힌트 뒤 스스로 맞힌 비율'에서 뺄 때 쓴다."""

    progress = models.ForeignKey(LessonProgress, on_delete=models.CASCADE, related_name="reveals")
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="reveals")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["progress", "question"], name="unique_reveal_per_question"),
        ]


class LessonChunk(models.Model):
    """사수 힌트가 근거로 찾을 레슨 본문 조각 (FR-17, WBS 4.1).
    본문(body_md)만 자른다. 문제 해설은 정답이 들어 있어 넣지 않는다."""

    lesson = models.ForeignKey(Lesson, on_delete=models.CASCADE, related_name="chunks")
    chunk_index = models.PositiveIntegerField()
    content = models.TextField()
    embedding = VectorField(dimensions=EMBEDDING_DIMENSIONS)
    # 레슨 제목+본문의 지문(해시). 같으면 다시 임베딩하지 않아 비용을 아낀다
    source_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["lesson", "chunk_index"]
        constraints = [
            models.UniqueConstraint(fields=["lesson", "chunk_index"], name="unique_chunk_order"),
        ]

    def __str__(self):
        return f"{self.lesson.order_no}-{self.chunk_index}"


class HintRequest(models.Model):
    """사수 힌트 1회 (FR-08, WBS 4.2).
    하루 한도(20회)와 '힌트를 받은 뒤 스스로 맞힌 비율'은 이 테이블에서 센다."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
        related_name="hint_requests",
    )
    progress = models.ForeignKey(LessonProgress, on_delete=models.CASCADE, related_name="hints")
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="hints")
    level = models.PositiveSmallIntegerField()  # 1 방향 → 2 위치 → 3 개념
    response = models.TextField()
    source_lessons = models.JSONField(default=list)  # [{"lesson_id": 1, "title": "..."}]
    blocked = models.BooleanField(default=False)  # 정답 노출로 답변을 막고 고정 문구를 보냈는지
    model = models.CharField(max_length=50)  # 어떤 모델이 만들었는지 (나중에 모델별 품질 비교)
    tokens_in = models.PositiveIntegerField(default=0)
    tokens_out = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(fields=["progress", "question", "level"], name="unique_hint_level"),
        ]


def random_diag_order():
    """사전·사후 진단 순서를 사람마다 무작위로 정한다. AB면 사전 A·사후 B (FR-15)"""
    import secrets

    return secrets.choice(["AB", "BA"])


class Profile(models.Model):
    """사용자 설정 (WBS 5.1, FR-02). 처음 로그인하면 주간 목표를 고른다.
    알림 시각(notify_time)은 5.2 알림에서 쓴다. 행은 처음 필요할 때 만든다(get_profile).
    진단 순서(diag_order)는 A·B 세트 난이도 차이가 결과를 흔들지 않게 사람마다 반대로 배정한다 (FR-15)."""

    class DiagOrder(models.TextChoices):
        AB = "AB", "사전 A → 사후 B"
        BA = "BA", "사전 B → 사후 A"

    class Goal(models.IntegerChoices):
        LIGHT = 3, "주 3일"
        WEEKDAYS = 5, "주 5일"
        DAILY = 7, "매일"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    weekly_goal_days = models.PositiveSmallIntegerField(choices=Goal.choices, default=Goal.WEEKDAYS)
    notify_time = models.TimeField(null=True, blank=True)
    timezone = models.CharField(max_length=50, default="Asia/Seoul")
    onboarded_at = models.DateTimeField(null=True, blank=True)  # 목표를 처음 고른 때. 비어 있으면 고르는 화면부터
    diag_order = models.CharField(max_length=2, choices=DiagOrder.choices, default=random_diag_order)
    pre_diag_skipped_at = models.DateTimeField(null=True, blank=True)  # 사전 진단을 건너뛴 때 (베타 분석에서 뺀다)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user} - 주 {self.weekly_goal_days}일"


class LessonView(models.Model):
    """레슨 화면을 연 기록 (WBS 6.1 단계별 이탈). 풀이 시작·답·완료는 다른 표에 이미 남으므로
    '열어 보고 문제 풀기를 누르지 않은 사람'만 여기서 센다. 리포트: python manage.py funnel"""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True, related_name="lesson_views",
    )
    guest_key = models.CharField(max_length=64, blank=True)
    lesson = models.ForeignKey(Lesson, on_delete=models.CASCADE, related_name="views")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["lesson", "created_at"])]


class PushSubscription(models.Model):
    """웹 푸시 구독 1개 = 브라우저(기기) 1개 (WBS 5.2, FR-14).
    endpoint는 구글·애플 등 푸시 서비스 주소이고, p256dh·auth는 그 브라우저만 풀 수 있게 암호화할 때 쓰는 키다"""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="push_subscriptions")
    endpoint = models.CharField(max_length=1000, unique=True)
    p256dh = models.CharField(max_length=200)
    auth = models.CharField(max_length=100)
    user_agent = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.user} - {self.endpoint[:40]}"


class PushDelivery(models.Model):
    """하루 알림 1회 기록. 베타 분석에서 '알림을 받은 날 레슨을 풀었는지', '알림을 눌렀는지'를 본다"""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="push_deliveries")
    sent_on = models.DateField()  # 한국 날짜
    sent_at = models.DateTimeField(auto_now_add=True)
    ok_count = models.PositiveSmallIntegerField(default=0)  # 보낸 기기 수
    opened_at = models.DateTimeField(null=True, blank=True)  # 알림을 눌러 앱을 연 때

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "sent_on"], name="unique_push_per_day")]
