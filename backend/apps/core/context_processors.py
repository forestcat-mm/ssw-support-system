"""業務画面の共通の表示（ナビ・自機関名）。"""

from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.http import HttpRequest
from django.urls import NoReverseMatch, reverse
from django.utils.translation import gettext_lazy as _

from apps.core.models import Action
from services import authorization, sessions
from services.organization import organization_display_name


@dataclass(frozen=True)
class NavItem:
    label: Any
    url_name: str
    action: Action | None
    match: str | tuple[str, ...]  # この接頭辞の画面でアクティブにする（"" は完全一致）
    footer: bool = False  # サイドバーの下（設定）に置く
    debug_only: bool = False  # DEBUG のときだけ出す（部品の見本。ADR-0046）


# サイドバーの並び（仕様 5.1・見本 03〜07。U2-D2）。管理画面（admin）へのリンクは出さない
# （U2-D3。管理者は /admin/ を直接開く）。部品の見本は DEBUG のときだけ出す
# （/ui/ は今の権限で開ける）
NAV_ITEMS: tuple[NavItem, ...] = (
    # 届出タスクの処理（filings:）はダッシュボードから開くので、ダッシュボードをアクティブにする
    NavItem(_("Dashboard"), "home", Action.FILINGS_VIEW, ("home", "filings:")),
    NavItem(_("Foreign workers"), "workers:list", Action.WORKERS_VIEW, "workers:"),
    NavItem(_("Host organizations"), "hosts:list", Action.HOSTS_VIEW, "hosts:"),
    # 一括申請（T48）・一括届出（T50）
    NavItem(_("Bulk applications"), "forms:bulk_list", Action.FORMS_GENERATE, "forms:bulk"),
    NavItem(_("Bulk notifications"), "forms:efile_list", Action.FORMS_GENERATE, "forms:efile"),
    NavItem(_("Consultations"), "consultations:list", Action.SUPPORT_VIEW, "consultations:"),
    NavItem(
        _("UI components"), "ui_showcase", Action.SYSTEM_MANAGE, "ui_showcase", debug_only=True
    ),
    NavItem(_("Settings"), "settings:index", Action.USERS_MANAGE, "settings:", footer=True),
)


TIME_OPTIONS = [f"{minutes // 60:02d}:{minutes % 60:02d}" for minutes in range(0, 24 * 60, 5)]


def ui(request: HttpRequest) -> dict[str, Any]:
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"nav_items": []}
    match = getattr(request, "resolver_match", None)
    current = match.view_name if match else ""
    items: list[dict[str, Any]] = []
    footer: list[dict[str, Any]] = []
    for item in NAV_ITEMS:
        if item.action is not None and not authorization.has_action(user, item.action):
            continue
        if item.debug_only and not settings.DEBUG:
            continue
        try:
            url = reverse(item.url_name)
        except NoReverseMatch:
            continue  # まだ作っていない画面（T13b 以降で追加）
        prefixes = (item.match,) if isinstance(item.match, str) else item.match
        active = current == item.url_name if not item.match else current.startswith(prefixes)
        (footer if item.footer else items).append(
            {"label": item.label, "url": url, "active": active}
        )
    return {
        "nav_items": items,
        "nav_footer": footer,
        # 上の帯の全体の検索（U2-D1）: 外国人台帳の検索（氏名・在留カード番号・旅券番号）に送る
        "global_search_url": (
            reverse("workers:list") if authorization.has_action(user, Action.WORKERS_VIEW) else ""
        ),
        # 外国人台帳では、上の帯の検索に今の検索の言葉を入れたまま出し、ほかの絞り込みの条件も
        # 残して送る（一覧の中の検索の欄は出さない。U3 の直し）
        "global_search_value": request.GET.get("q", "") if current == "workers:list" else "",
        "global_search_keep": (
            [
                (key, value)
                for key, values in request.GET.lists()
                if key not in ("q", "page")
                for value in values
            ]
            if current == "workers:list"
            else []
        ),
        "organization_name": organization_display_name(),
        "session_timing": sessions.timing(request.session),  # 自動ログアウトの知らせ（J8）
        "time_options": TIME_OPTIONS,  # 時刻の欄の 5 分刻みの選択（datalist。修正 1 回目 B16）
    }
