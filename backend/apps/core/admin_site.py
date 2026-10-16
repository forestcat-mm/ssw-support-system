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
from services.organization import organization_display_name

# 管理者のメニュー（admin のトップと左のナビ）から隠すアプリ（T15a）。
# 権限は変えないので、権限のある人は直接の URL で開ける（README の「開発者向け」）。
HIDDEN_APPS = frozenset({"axes", "django_q", "seed"})

# 業務画面に移した項目もメニューから隠す（T15b。設定画面・外国人台帳・ダッシュボードで扱う）。
# 受入れ機関・雇用契約は業務画面がまだ閲覧だけなので、段階2の画面ができるまでメニューに残す。
HIDDEN_MODELS = frozenset(
    {
        ("core", "user"),
        ("core", "role"),
        ("core", "staff"),
        ("core", "office"),
        ("core", "organization"),
        ("core", "auditlog"),
        ("core", "attachment"),
        ("workers", "foreignworker"),
        ("filings", "filing"),
    }
)


class SupportAdminSite(admin.AdminSite):
    site_header = _("Support organization management")
    site_title = _("Support organization management")
    index_title = _("Management")
    site_url = None  # 「サイトを表示」は出さず、ヘッダーの「業務画面へ戻る」に一本化する

    def each_context(self, request: HttpRequest) -> dict[str, Any]:
        context = super().each_context(request)
        name = organization_display_name()
        context["site_header"] = _("%(name)s management site") % {"name": name}
        context["site_title"] = context["site_header"]
        return context

    def get_app_list(self, request: HttpRequest, app_label: str | None = None) -> list[Any]:
        """開発者向けの項目をメニューから隠す（直接の URL は今までどおり開ける）。"""
        result = []
        for app in super().get_app_list(request, app_label):
            if app["app_label"] in HIDDEN_APPS:
                continue
            app["models"] = [
                model
                for model in app["models"]
                if (app["app_label"], model["object_name"].lower()) not in HIDDEN_MODELS
            ]
            if not app["models"]:
                continue
            if app["app_label"] == "core":
                app["name"] = _("Masters (countries and support languages)")
            result.append(app)
        return result

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
