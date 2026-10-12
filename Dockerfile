# 코드사수 배포 이미지 (Google Cloud Run, 서울)
# Cloud Run이 이 파일대로 이미지를 만들어 실행한다. 비밀 값(.env)은 이미지에 넣지 않고
# Cloud Run 화면의 환경변수로 따로 넣는다 (.dockerignore 참고)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# CSS·JS·아이콘을 한곳(staticfiles/)에 모은다. 설정 파일이 환경변수를 요구해서 빌드용 가짜 값을 넣는다
RUN DJANGO_SECRET_KEY=build-only DATABASE_URL=postgres://build:build@localhost:5432/build \
    GOOGLE_CLIENT_ID=build GOOGLE_CLIENT_SECRET=build \
    python manage.py collectstatic --noinput

# 서버가 켜질 때: DB 구조를 최신으로 맞추고(migrate), 모든 표에 RLS를 켜고(ensure_rls),
# 환경변수 ADMIN_EMAILS의 구글 계정에 관리자 권한을 준 뒤
# Cloud Run이 알려 주는 포트($PORT, 보통 8080)로 gunicorn을 띄운다.
# migrate가 실패하면 새 버전이 뜨지 않고 Cloud Run은 이전 버전을 계속 쓴다. 서버는 최대 1대라 동시에 두 번 돌 일은 없다
CMD python manage.py migrate --noinput && python manage.py ensure_rls && python manage.py grant_admin --from-env && exec gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8080} --workers 2 --threads 4 --timeout 60 --access-logfile -
