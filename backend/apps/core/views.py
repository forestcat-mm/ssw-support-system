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

from apps.core.forms import PasswordResetRequestForm, SecondFactorForm, TokenForm
from apps.core.middleware import mfa_exempt
from apps.core.models import Attachment, AuditAction, RecoveryCodeDevice, User
from services import attachments, audit, invitations, mfa, password_reset

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
    """添付のダウンロード。ファイルの中身を渡す唯一の経路（公開 URL・署名付き URL は作らない）。

    判定は services.attachments.can_download（対象の閲覧権限。要配慮の添付は担当者と
    workers.sensitive_documents を持つ人だけ）。成功は download、要配慮の添付の拒否は
    download_denied を監査ログに残す（ADR-0011）。
    """

    def get(self, request: HttpRequest, pk: int) -> FileResponse:
        attachment = get_object_or_404(Attachment.objects.select_related("content_type"), pk=pk)
        if attachment.target is None:
            raise Http404
        content_type = attachment.content_type
        if not attachments.can_download(request.user, attachment):
            if attachment.is_sensitive:
                audit.record(
                    AuditAction.DOWNLOAD_DENIED,
                    target=attachment,
                    context={"kind": attachment.kind},
                )
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


# --- パスワードの再設定（T25）-------------------------------------------------------------


def _no_referrer(response: HttpResponse) -> HttpResponse:
    """リファラーを送らない（トークンを含むリンクの応答と、フォームの無い画面）。"""
    response["Referrer-Policy"] = "no-referrer"
    return response


def _same_origin(response: HttpResponse) -> HttpResponse:
    """フォームのある画面は same-origin にする。no-referrer にするとブラウザがフォームの送信に
    Origin: null を付け、CSRF の Origin の検査で拒否されるため。これらの画面の URL には
    トークンを含まない（リンクを開いたらトークンをセッションに移してリダイレクトする）。"""
    response["Referrer-Policy"] = "same-origin"
    return response


@mfa_exempt
class PasswordResetRequestView(View):
    """申請（メールアドレス）。アカウントの有無で処理も結果も変えない（送信はジョブ）。"""

    template_name = "accounts/password_reset_request.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect("home")
        return _same_origin(
            render(request, self.template_name, {"form": PasswordResetRequestForm()})
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        form = PasswordResetRequestForm(request.POST)
        if not form.is_valid():
            return _same_origin(render(request, self.template_name, {"form": form}))
        password_reset.request_reset(request, form.cleaned_data["email"])
        return redirect("accounts:password_reset_done")


@mfa_exempt
class PasswordResetDoneView(View):
    def get(self, request: HttpRequest) -> HttpResponse:
        return _no_referrer(render(request, "accounts/password_reset_done.html"))


@mfa_exempt
class PasswordResetOpenView(View):
    """メールのリンク。トークンをセッションに移し、トークンを含まない URL へリダイレクトする。"""

    def get(self, request: HttpRequest, token: str) -> HttpResponse:
        request.session[password_reset.SESSION_KEY] = token
        return _no_referrer(redirect("accounts:password_reset_set"))


@mfa_exempt
class PasswordResetSetView(View):
    """新しいパスワード（2 回）。トークンはセッションから読む。"""

    template_name = "accounts/password_reset_set.html"

    def reset(self, request: HttpRequest) -> Any:
        token = request.session.get(password_reset.SESSION_KEY, "")
        return password_reset.find_valid(token) if token else None

    def invalid(self, request: HttpRequest) -> HttpResponse:
        request.session.pop(password_reset.SESSION_KEY, None)
        return _no_referrer(render(request, "accounts/password_reset_invalid.html", status=400))

    def get(self, request: HttpRequest) -> HttpResponse:
        reset = self.reset(request)
        if reset is None:
            return self.invalid(request)
        form = SetPasswordForm(reset.user)
        return _same_origin(render(request, self.template_name, {"form": form}))

    def post(self, request: HttpRequest) -> HttpResponse:
        reset = self.reset(request)
        if reset is None:
            return self.invalid(request)
        form = SetPasswordForm(reset.user, request.POST)
        if form.is_valid():
            token = request.session.pop(password_reset.SESSION_KEY)
            completed = password_reset.complete(token, form.cleaned_data["new_password1"])
            if completed is None:
                return self.invalid(request)
            request.session["password_reset_lock_minutes"] = completed.lock_minutes
            return redirect("accounts:password_reset_complete")
        return _same_origin(render(request, self.template_name, {"form": form}))


@mfa_exempt
class PasswordResetCompleteView(View):
    def get(self, request: HttpRequest) -> HttpResponse:
        context = {"lock_minutes": request.session.pop("password_reset_lock_minutes", None)}
        return _no_referrer(render(request, "accounts/password_reset_complete.html", context))
