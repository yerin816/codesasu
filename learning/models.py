from django.conf import settings
from django.db import models
from pgvector.django import VectorField

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
