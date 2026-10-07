"""가입·로그인하면 게스트로 푼 첫 레슨 기록을 그 계정에 붙인다 (FR-01)"""

from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver

from .models import AnswerAttempt, LessonProgress


@receiver(user_logged_in)
def attach_guest_records(sender, request, user, **kwargs):
    if request is None:
        return
    guest_key = request.session.get("guest_key")
    if not guest_key:
        return
    progresses = LessonProgress.objects.filter(guest_key=guest_key, user__isnull=True)
    AnswerAttempt.objects.filter(progress__in=progresses, user__isnull=True).update(user=user)
    progresses.update(user=user)
    # 한 번 붙였으면 같은 브라우저에서 다른 계정으로 로그인해도 다시 붙지 않게 지운다
    del request.session["guest_key"]
