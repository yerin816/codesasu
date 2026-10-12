"""구글 계정으로 가입한 사용자에게 관리자 권한을 준다.

    python manage.py grant_admin you@gmail.com     # 이메일을 직접 지정
    python manage.py grant_admin --from-env        # 환경변수 ADMIN_EMAILS(쉼표 구분)에서 읽기

관리자 로그인은 일반 로그인 화면을 거치는데, 비밀번호 대신 구글 로그인(2단계 인증)을 쓰려고
구글 계정이 연결된 사용자에게만 권한을 준다.
--from-env는 배포 서버가 켜질 때 실행된다(Dockerfile). 대상이 없어도 서버 시작을 막지 않는다.
"""

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


def google_users(email):
    return get_user_model().objects.filter(
        email__iexact=email.strip(),
        socialaccount__provider="google",
    ).distinct()


class Command(BaseCommand):
    help = "구글 계정이 연결된 사용자에게 관리자 권한을 준다"

    def add_arguments(self, parser):
        parser.add_argument("email", nargs="?")
        parser.add_argument("--from-env", action="store_true", help="환경변수 ADMIN_EMAILS에서 이메일을 읽는다")

    def grant(self, user):
        if not (user.is_staff and user.is_superuser):
            user.is_staff = True
            user.is_superuser = True
            user.save(update_fields=["is_staff", "is_superuser"])
        self.stdout.write(self.style.SUCCESS(f"관리자 권한을 줬어요: 사용자 번호 {user.pk}"))

    def handle(self, *args, email=None, from_env=False, **options):
        if from_env:
            # 서버 시작 중이므로 실패해도 예외를 내지 않고 기록만 남긴다
            emails = [e for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()]
            for e in emails:
                users = google_users(e)
                if users.count() == 1:
                    self.grant(users.get())
                else:
                    self.stdout.write(f"관리자 지정 건너뜀: 구글 계정이 연결된 사용자 {users.count()}명")
            return

        if not email:
            raise CommandError("이메일을 적거나 --from-env를 붙여 주세요.")
        users = google_users(email)
        if users.count() != 1:
            raise CommandError(
                f"구글 계정이 연결된 '{email}' 사용자를 {users.count()}명 찾았어요. "
                "배포 주소에서 '구글로 계속하기'로 한 번 로그인한 뒤 다시 실행해 주세요."
            )
        self.grant(users.get())
