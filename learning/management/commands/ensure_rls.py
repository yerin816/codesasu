"""Supabase(PostgreSQL)의 public 표 전부에 RLS(행 수준 보안)를 켠다.

    python manage.py ensure_rls

왜: Supabase는 public 표를 외부 API(Data API)로 열 수 있는데, RLS가 꺼진 표는 키만 있으면 읽고 쓸 수 있다.
코드사수는 Django 서버만 DB에 접근하므로, 정책 없이 RLS만 켜 두면 외부 API로는 아무 행도 보이지 않는다.

Django 서버는 영향이 없다: PostgreSQL에서 RLS를 켤 수 있는 건 표 주인뿐이고,
표 주인은 (FORCE를 걸지 않는 한) RLS를 거치지 않는다. 즉 이 명령이 성공했다면 Django가 표 주인이다.

배포 서버가 켜질 때마다 실행된다(Dockerfile). 새 마이그레이션으로 생긴 표도 다음 배포에서 자동으로 켜진다.
실패해도 서버 시작은 막지 않고 기록만 남긴다. SQLite(테스트·PC 개발)에서는 아무것도 하지 않는다.
"""

from django.core.management.base import BaseCommand
from django.db import connection, transaction


class Command(BaseCommand):
    help = "public 스키마의 모든 표에 RLS를 켠다 (PostgreSQL 전용, 여러 번 실행해도 안전)"

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            self.stdout.write("RLS: PostgreSQL이 아니라서 건너뜀")
            return

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND NOT rowsecurity ORDER BY tablename"
            )
            tables = [row[0] for row in cursor.fetchall()]

        enabled, failed = [], []
        for table in tables:
            try:
                with transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute(f"ALTER TABLE public.{connection.ops.quote_name(table)} ENABLE ROW LEVEL SECURITY")
                enabled.append(table)
            except Exception as exc:  # 주인이 아닌 표 등. 서버 시작은 계속한다
                failed.append(table)
                self.stderr.write(f"RLS 켜기 실패: {table} ({type(exc).__name__})")

        self.stdout.write(f"RLS: 새로 켬 {len(enabled)}개, 실패 {len(failed)}개")
