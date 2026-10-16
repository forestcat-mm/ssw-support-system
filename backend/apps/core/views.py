"""ログイン・多要素認証の画面。判定・検証は services に置き、ここは入出力だけを扱う。"""

from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.contrib.auth.signals import user_login_failed
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views import View
from django_otp import login as otp_login

from apps.core.forms import SecondFactorForm, TokenForm
from apps.core.middleware import mfa_exempt
from apps.core.models import RecoveryCodeDevice, User
from services import mfa

NEW_RECOVERY_CODES_SESSION_KEY = "new_recovery_codes"


def _next_url(request: HttpRequest) -> str:
    candidate = request.POST.get("next") or request.GET.get("next") or ""
    if url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return candidate
    return str(settings.LOGIN_REDIRECT_URL)


def _with_next(name: str, request: HttpRequest) -> str:
    return f"{reverse(name)}?{urlencode({'next': _next_url(request)})}"


def _user(request: HttpRequest) -> User:
    assert isinstance(request.user, User)
    return request.user


@mfa_exempt
class LoginView(auth_views.LoginView):
    template_name = "accounts/login.html"
    redirect_authenticated_user = True


@mfa_exempt
class LogoutView(auth_views.LogoutView):
    next_page = "accounts:login"


@mfa_exempt
@method_decorator(login_required, name="dispatch")
class MFASetupView(View):
    """初回の TOTP 設定。確定済みの端末がある場合は再設定させない（リセットは管理者が行う）。"""

    template_name = "accounts/mfa_setup.html"

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if mfa.has_confirmed_totp(_user(request)):
            return redirect(_with_next("accounts:mfa_verify", request))
        return super().dispatch(request, *args, **kwargs)

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._render(request, TokenForm())

    def post(self, request: HttpRequest) -> HttpResponse:
        form = TokenForm(request.POST)
        device = mfa.pending_totp_device(_user(request))
        if form.is_valid():
            codes = mfa.confirm_totp(device, form.cleaned_data["token"])
            if codes is not None:
                otp_login(request, device)
                request.session[NEW_RECOVERY_CODES_SESSION_KEY] = codes
                return redirect(_with_next("accounts:recovery_codes", request))
            form.add_error("token", _("The code is incorrect. Check the time on your device."))
        return self._render(request, form)

    def _render(self, request: HttpRequest, form: TokenForm) -> HttpResponse:
        device = mfa.pending_totp_device(_user(request))
        url = mfa.totp_config_url(device)
        context = {
            "form": form,
            "qr_svg": mfa.qr_svg(url),
            "secret": mfa.totp_secret(device),
            "next": _next_url(request),
        }
        return render(request, self.template_name, context)


@mfa_exempt
@method_decorator(login_required, name="dispatch")
class MFAVerifyView(View):
    """ログイン時の TOTP（または回復用コード）入力。失敗はログイン失敗としてロック回数に数える。"""

    template_name = "accounts/mfa_verify.html"

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if not mfa.has_confirmed_totp(_user(request)):
            return redirect(_with_next("accounts:mfa_setup", request))
        return super().dispatch(request, *args, **kwargs)

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._render(request, SecondFactorForm())

    def post(self, request: HttpRequest) -> HttpResponse:
        user = _user(request)
        form = SecondFactorForm(request.POST)
        if form.is_valid():
            device = mfa.verify_second_factor(user, form.cleaned_data["code"])
            if device is not None:
                otp_login(request, device)
                if isinstance(device, RecoveryCodeDevice):
                    messages.warning(
                        request,
                        _("You signed in with a recovery code. %(count)d codes remain.")
                        % {"count": mfa.remaining_recovery_codes(user)},
                    )
                return redirect(_next_url(request))
            form.add_error("code", _("The code is incorrect."))
        user_login_failed.send(
            sender=__name__, credentials={"username": user.get_username()}, request=request
        )
        if getattr(request, "axes_locked_out", False):
            logout(request)  # AxesMiddleware がロック画面を返す
        return self._render(request, form)

    def _render(self, request: HttpRequest, form: SecondFactorForm) -> HttpResponse:
        return render(request, self.template_name, {"form": form, "next": _next_url(request)})


@method_decorator(login_required, name="dispatch")
class RecoveryCodesView(View):
    """回復用コードの表示（発行直後の 1 回だけ）と作り直し。TOTP 検証済みのときだけ開ける。"""

    template_name = "accounts/recovery_codes.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        context = {
            "codes": request.session.pop(NEW_RECOVERY_CODES_SESSION_KEY, None),
            "remaining": mfa.remaining_recovery_codes(_user(request)),
            "next": _next_url(request),
        }
        return render(request, self.template_name, context)

    def post(self, request: HttpRequest) -> HttpResponse:
        request.session[NEW_RECOVERY_CODES_SESSION_KEY] = mfa.issue_recovery_codes(_user(request))
        return redirect("accounts:recovery_codes")
