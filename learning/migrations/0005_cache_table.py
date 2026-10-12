"""로그인 시도 제한용 캐시 표(django_cache)를 만든다.

settings.CACHES가 DB 캐시를 쓰므로, 표가 없으면 로그인 화면에서 오류가 난다.
`python manage.py migrate` 한 번으로 배포 DB에도 같이 만들어지게 마이그레이션에 넣었다.
"""

from django.core.management import call_command
from django.db import migrations


def create_cache_table(apps, schema_editor):
    call_command("createcachetable", database=schema_editor.connection.alias, verbosity=0)


class Migration(migrations.Migration):
    dependencies = [("learning", "0004_hintrequest")]

    operations = [migrations.RunPython(create_cache_table, migrations.RunPython.noop)]
