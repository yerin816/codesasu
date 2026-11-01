"""하루 1번 알림: 누구에게, 언제, 무엇을 보낼지 (WBS 5.2, FR-14)

Cloud Scheduler가 30분마다 /internal/push/send-due 를 부르면 send_due()가 돈다.
보내는 조건 (모두 만족할 때만)
- 알림 시각을 정했고 구독한 기기가 있다
- 지금(한국 시간)이 알림 시각과 같거나 지났고, 지난 지 1시간이 안 됐다 (예약 실행이 한 번 빠져도 다음 실행이 보냄)
- 오늘 아직 알림을 받지 않았다 (PushDelivery가 하루 한 줄)
- 오늘 아직 레슨을 풀지 않았다 (이미 푼 사람에게는 보내지 않는다)
- 풀 레슨이 남아 있다
"""

import logging
from datetime import datetime, timedelta

from django.db import IntegrityError
from django.utils import timezone

from . import webpush
from .models import Lesson, LessonProgress, Profile, PushDelivery, PushSubscription

logger = logging.getLogger(__name__)

SEND_WINDOW = timedelta(hours=1)


def next_lesson(user):
    from .views import published_lessons  # 순환 import 방지

    done_ids = LessonProgress.objects.filter(user=user, completed_at__isnull=False).values_list("lesson_id", flat=True)
    return published_lessons().exclude(id__in=done_ids).first()


def studied_today(user, today):
    return LessonProgress.objects.filter(
        user=user, completed_at__date=today, lesson__kind=Lesson.Kind.LESSON
    ).exists()


def lesson_payload(lesson):
    return {
        "title": "코드사수 · 오늘의 레슨",
        "body": f"{lesson.title} (약 {lesson.est_minutes}분)",
        "url": "/?from=push",
    }


def send_to_user(user, payload):
    """사용자의 모든 기기에 보낸다. 사라진 구독은 지운다. (보낸 수, 지운 수)"""
    ok = removed = 0
    for sub in list(user.push_subscriptions.all()):
        result = webpush.send(sub, payload)
        if result == "ok":
            ok += 1
            PushSubscription.objects.filter(id=sub.id).update(last_sent_at=timezone.now())
        elif result == webpush.GONE:
            sub.delete()
            removed += 1
        else:
            logger.warning("알림 보내기 실패 (구독 %s): %s", sub.id, result)
    return ok, removed


def due_profiles(now=None):
    now = timezone.localtime(now or timezone.now())
    today = now.date()
    profiles = (Profile.objects.filter(notify_time__isnull=False, user__push_subscriptions__isnull=False)
                .select_related("user").distinct())
    for profile in profiles:
        at = timezone.make_aware(datetime.combine(today, profile.notify_time), now.tzinfo)
        if not (at <= now < at + SEND_WINDOW):
            continue
        if PushDelivery.objects.filter(user=profile.user, sent_on=today).exists():
            continue
        if studied_today(profile.user, today):
            continue
        yield profile


def send_due(now=None):
    """지금 보낼 사람에게 알림을 보내고 요약을 돌려준다"""
    today = timezone.localdate(now or timezone.now())
    summary = {"due": 0, "sent": 0, "skipped_no_lesson": 0, "removed": 0}
    for profile in due_profiles(now):
        lesson = next_lesson(profile.user)
        if lesson is None:
            summary["skipped_no_lesson"] += 1
            continue
        summary["due"] += 1
        # 하루 한 줄을 먼저 만든다. 예약 실행이 겹쳐도 두 번 보내지 않게
        try:
            delivery = PushDelivery.objects.create(user=profile.user, sent_on=today)
        except IntegrityError:
            continue
        ok, removed = send_to_user(profile.user, lesson_payload(lesson))
        delivery.ok_count = ok
        delivery.save(update_fields=["ok_count"])
        summary["sent"] += ok
        summary["removed"] += removed
    return summary
