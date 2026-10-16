"""管理サイト。入場は is_staff ではなく、ロールの操作（admin.access）と TOTP 検証で判定する。

ログインは accounts:login（パスワード → TOTP）に一本化し、admin のログイン画面は使わない。
"""

from typing import Any

from django.contrib import admin
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from apps.core.models import Action
from services import authorization, mfa


class SupportAdminSite(admin.AdminSite):
    site_header = _("Support organization management")
    site_title = _("Support organization management")
    index_title = _("Management")

    def has_permission(self, request: HttpRequest) -> bool:
        user = request.user
        return mfa.is_verified(user) and authorization.has_action(user, Action.ADMIN_ACCESS)

    def login(self, request: HttpRequest, extra_context: Any = None) -> HttpResponse:
        if request.user.is_authenticated:
            # ログイン済み（TOTP 検証済み）で admin.access がない。
            # ログイン画面へ戻すと循環するため 403 にする。
            raise PermissionDenied
        next_url = request.GET.get("next") or reverse("admin:index")
        return redirect_to_login(next_url, reverse("accounts:login"))
