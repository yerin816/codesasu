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

# Cloud Run이 알려 주는 포트($PORT, 보통 8080)로 gunicorn을 띄운다
CMD exec gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8080} --workers 2 --threads 4 --timeout 60 --access-logfile -
