from django.http import HttpResponse
from django.utils.html import escape


def home(request):
    """로그인 확인용 임시 홈 화면. 3.3에서 진짜 화면으로 바꾼다."""
    if request.user.is_authenticated:
        return HttpResponse(
            f"로그인됨: {escape(request.user.email)} "
            '<a href="/accounts/logout/">로그아웃</a>'
        )
    return HttpResponse('로그인 안 됨. <a href="/accounts/login/">로그인하기</a>')