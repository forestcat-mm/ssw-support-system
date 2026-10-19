"""多要素認証の強制・監査ログのリクエスト文脈・モーダルの送信・ログインからの上限。"""

from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.views import redirect_to_login
from django.http import HttpRequest, HttpResponse
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.core.models import User
from services import audit, sessions
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


class SessionLimitMiddleware:
    """ログインからの上限（既定 12 時間）を過ぎたセッションをログアウトする（修正 1 回目 J8）。

    無操作の失効はセッションの期限（SESSION_COOKIE_AGE）が担う。画面の外の要求（延長の通信・
    モーダルの中の送信）には 401 を返し、ほかはログインの画面に移す。
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated and sessions.absolute_remaining(request.session) <= 0:
            logout(request)
            messages.info(
                request,
                _(
                    "You were signed out because %(hours)d hours have passed since you signed "
                    "in. Sign in again."
                )
                % {"hours": settings.SESSION_ABSOLUTE_TIMEOUT // 3600},
            )
            if request.headers.get("X-Modal") == "1" or request.path == reverse(
                "accounts:keepalive"
            ):
                return HttpResponse(status=401)
            return redirect_to_login(request.get_full_path())
        return self.get_response(request)
