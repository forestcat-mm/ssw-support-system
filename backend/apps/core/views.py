"""ログイン・多要素認証の画面。判定・検証は services に置き、ここは入出力だけを扱う。"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.auth.signals import user_login_failed
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views import View
from django_otp import login as otp_login

from apps.core.forms import SecondFactorForm, TokenForm
from apps.core.middleware import mfa_exempt
from apps.core.models import Attachment, AuditAction, RecoveryCodeDevice, User
from services import audit, authorization, invitations, mfa

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
        request.session[NEW_RECOVERY_CODES_SESSION_KEY] = mfa.regenerate_recovery_codes(
            _user(request)
        )
        return redirect("accounts:recovery_codes")


@mfa_exempt
class InvitationAcceptView(View):
    """招待リンク。パスワードを設定させてログインし、TOTP の設定へ進める。"""

    template_name = "accounts/invitation_accept.html"

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        invitation = invitations.find_valid_invitation(kwargs["token"])
        if invitation is None:
            return render(request, "accounts/invitation_invalid.html", status=400)
        if request.user.is_authenticated:
            logout(request)
        self.invitation = invitation
        return super().dispatch(request, *args, **kwargs)

    def get(self, request: HttpRequest, token: str) -> HttpResponse:
        return self._render(request, SetPasswordForm(self.invitation.user))

    def post(self, request: HttpRequest, token: str) -> HttpResponse:
        form = SetPasswordForm(self.invitation.user, request.POST)
        if form.is_valid():
            user = invitations.accept_invitation(
                self.invitation, form.cleaned_data["new_password1"]
            )
            login(request, user, backend="apps.core.backends.RoleBackend")
            return redirect("accounts:mfa_setup")
        return self._render(request, form)

    def _render(self, request: HttpRequest, form: SetPasswordForm[User]) -> HttpResponse:
        context = {"form": form, "invited_user": self.invitation.user}
        return render(request, self.template_name, context)


def invitation_url_builder(request: HttpRequest) -> invitations.UrlBuilder:
    def build(token: str) -> str:
        return request.build_absolute_uri(reverse("accounts:invitation_accept", args=[token]))

    return build


@method_decorator(login_required, name="dispatch")
class AttachmentDownloadView(View):
    """添付のダウンロード。紐づく対象の閲覧権限で判定し、監査ログに残す。公開 URL は作らない。"""

    def get(self, request: HttpRequest, pk: int) -> FileResponse:
        attachment = get_object_or_404(Attachment.objects.select_related("content_type"), pk=pk)
        target = attachment.target
        if target is None:
            raise Http404
        content_type = attachment.content_type
        perm = f"{content_type.app_label}.view_{content_type.model}"
        if not authorization.has_django_permission(request.user, perm, target):
            raise PermissionDenied
        response = FileResponse(
            attachment.file.open("rb"), as_attachment=True, filename=attachment.original_filename
        )
        audit.record(
            AuditAction.DOWNLOAD,
            target=attachment,
            context={
                "target": f"{content_type.app_label}.{content_type.model}:{attachment.object_id}"
            },
        )
        return response


@method_decorator(login_required, name="dispatch")
class KeepAliveView(View):
    """長い入力の画面で、入力している間のセッションを延ばす（無操作の失効で入力が消えないように）。

    SESSION_SAVE_EVERY_REQUEST により、このリクエスト自体が操作として扱われ失効が延びる。
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        return HttpResponse(status=204)
