"""業務画面の共通の表示（ナビ・自機関名）。"""

from dataclasses import dataclass
from typing import Any

from django.http import HttpRequest
from django.urls import NoReverseMatch, reverse
from django.utils.translation import gettext_lazy as _

from apps.core.models import Action
from services import authorization
from services.organization import organization_display_name


@dataclass(frozen=True)
class NavItem:
    label: Any
    url_name: str
    action: Action | None
    match: str | tuple[str, ...]  # この接頭辞の画面でアクティブにする（"" は完全一致）


NAV_ITEMS: tuple[NavItem, ...] = (
    # 届出タスクの処理（filings:）はダッシュボードから開くので、ダッシュボードをアクティブにする
    NavItem(_("Dashboard"), "home", Action.FILINGS_VIEW, ("home", "filings:")),
    NavItem(_("Foreign workers"), "workers:list", Action.WORKERS_VIEW, "workers:"),
    NavItem(_("Host organizations"), "hosts:list", Action.HOSTS_VIEW, "hosts:"),
    NavItem(_("Consultations"), "consultations:list", Action.SUPPORT_VIEW, "consultations:"),
    NavItem(_("Settings"), "settings:index", Action.USERS_MANAGE, "settings:"),
    NavItem(_("Management site"), "admin:index", Action.USERS_MANAGE, "admin:index"),
    NavItem(_("UI components"), "ui_showcase", Action.SYSTEM_MANAGE, "ui_showcase"),
)


def ui(request: HttpRequest) -> dict[str, Any]:
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"nav_items": []}
    match = getattr(request, "resolver_match", None)
    current = match.view_name if match else ""
    items = []
    for item in NAV_ITEMS:
        if item.action is not None and not authorization.has_action(user, item.action):
            continue
        try:
            url = reverse(item.url_name)
        except NoReverseMatch:
            continue  # まだ作っていない画面（T13b 以降で追加）
        prefixes = (item.match,) if isinstance(item.match, str) else item.match
        active = current == item.url_name if not item.match else current.startswith(prefixes)
        items.append({"label": item.label, "url": url, "active": active})
    return {"nav_items": items, "organization_name": organization_display_name()}
