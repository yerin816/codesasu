"""화면(HTML) 뷰. 데이터는 화면의 JavaScript가 /api/v1/ API로 가져온다 (설계: Django 템플릿 + 약간의 JS)"""

from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET


@ensure_csrf_cookie  # 화면의 JS가 POST할 때 쓸 CSRF 쿠키를 처음부터 받아 둔다
@require_GET
def home(request):
    """홈: 오늘의 레슨 1개와 이번 주 진행"""
    return render(request, "learning/home.html")


@ensure_csrf_cookie
@require_GET
def lesson(request, lesson_id):
    """레슨 풀이: 본문 → 문제 → 결과 1장. 권한 확인은 API가 한다"""
    return render(request, "learning/lesson.html", {"lesson_id": lesson_id})


@require_GET
def manifest(request):
    """PWA 설치 정보. 홈 화면 아이콘·이름·주소창 없이 열기 (FR-13)"""
    return render(
        request, "learning/manifest.webmanifest", content_type="application/manifest+json"
    )


@never_cache  # 서비스 워커 파일은 브라우저가 항상 새로 받아야 수정이 반영된다
@require_GET
def service_worker(request):
    """서비스 워커는 사이트 맨 위 주소(/sw.js)에 있어야 모든 화면을 맡을 수 있다"""
    return render(request, "learning/sw.js", content_type="application/javascript")
