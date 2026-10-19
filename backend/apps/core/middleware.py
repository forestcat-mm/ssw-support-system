"""多要素認証の強制と、監査ログのリクエスト文脈。"""

from collections.abc import Callable
from typing import Any

from django.contrib.auth.views import redirect_to_login
from django.http import HttpRequest, HttpResponse
from django.urls import reverse

from apps.core.models import User
from services import audit
from services.mfa import has_confirmed_totp, is_verified


def mfa_exempt[F: Callable[..., Any]](view: F) -> F:
    """TOTP 検証前でも開けるビュー（ログイン・ログアウト・MFA 設定/検証・招待受諾）。"""
    view.mfa_exempt = True  # type: ignore[attr-defined]
    return view


class MFARequiredMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        return self.get_response(request)

    def process_view(
        self,
        request: HttpRequest,
        view_func: Callable[..., Any],
        view_args: Any,
        view_kwargs: Any,
    ) -> HttpResponse | None:
        user = request.user
        if not user.is_authenticated or is_verified(user) or _is_exempt(view_func):
            return None
        assert isinstance(user, User)
        target = "accounts:mfa_verify" if has_confirmed_totp(user) else "accounts:mfa_setup"
        return redirect_to_login(request.get_full_path(), reverse(target))


def _is_exempt(view_func: Callable[..., Any]) -> bool:
    view_class = getattr(view_func, "view_class", None)
    return bool(getattr(view_func, "mfa_exempt", False) or getattr(view_class, "mfa_exempt", False))


class AuditContextMiddleware:
    """リクエスト中の監査ログに操作者・接続元 IP・パスを付ける。"""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        with audit.bind_request(request):
            return self.get_response(request)


class ModalRedirectMiddleware:
    """モーダルの中の送信（X-Modal の要求）のリダイレクトを、204 と X-Modal-Location に置き換える。

    fetch はリダイレクトを自動でたどり、移り先の画面（とメッセージ）を読んでしまう。
    移り先を JS に渡し、画面の読み直しか移動を JS が決める（forms.js の initModal。
    修正 1 回目 B10）。
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if request.headers.get("X-Modal") == "1" and response.status_code in (301, 302, 303):
            location = response["Location"]
            response = HttpResponse(status=204)
            response["X-Modal-Location"] = location
        return response
