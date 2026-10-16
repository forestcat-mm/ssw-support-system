"""業務画面の共通部分（トップ・ナビ・部品の見本・権限エラー・バッジ）。"""

import re
from datetime import date, timedelta
from pathlib import Path

import pytest
from django.template import Context, Template
from django.test import Client

from services import deadlines
from tests.core.factories import make_user, verified_client

pytestmark = pytest.mark.django_db


def test_home_after_sign_in_uses_business_layout(support_staff_client: Client) -> None:
    page = support_staff_client.get("/")
    assert page.status_code == 200
    html = page.content.decode()
    assert '<link rel="stylesheet" href="/static/ui/tokens.css">' in html
    assert 'aria-current="page"' in html  # ダッシュボードがアクティブ
    assert "ログアウト" in html


def test_navigation_depends_on_role(admin_client: Client, support_staff_client: Client) -> None:
    staff_nav = [item["label"] for item in support_staff_client.get("/").context["nav_items"]]
    admin_nav = [item["label"] for item in admin_client.get("/").context["nav_items"]]
    assert "管理画面" not in [str(label) for label in staff_nav]
    assert {"管理画面", "部品の見本"} <= {str(label) for label in admin_nav}


def test_showcase_is_admin_only(admin_client: Client, support_staff_client: Client) -> None:
    page = admin_client.get("/ui/")
    assert page.status_code == 200
    html = page.content.decode()
    for level in ("overdue", "within_7", "within_14", "open", "not_yet"):
        assert f'data-level="{level}"' in html
    assert 'lang="zh-Hans"' in html
    denied = support_staff_client.get("/ui/")
    assert denied.status_code == 403
    assert "権限がありません" in denied.content.decode()


def test_anonymous_is_sent_to_login() -> None:
    response = Client().get("/")
    assert response.status_code == 302
    assert response["Location"].startswith("/accounts/login/")


def test_unverified_user_is_sent_to_mfa() -> None:
    client = Client()
    client.force_login(make_user("tanto", roles=("support_staff",)))
    assert client.get("/")["Location"].startswith("/accounts/mfa/")


@pytest.mark.parametrize(
    ("days", "css", "text"),
    [
        (-5, "badge--overdue", "超過 5日"),
        (0, "badge--within-7", "今日まで"),
        (3, "badge--within-7", "あと3日"),
        (12, "badge--within-14", "あと12日"),
        (60, "badge--open", "あと60日"),
    ],
)
def test_level_badge(days: int, css: str, text: str) -> None:
    today = date(2026, 10, 16)
    deadline = deadlines.residence_expiry(today + timedelta(days=days))
    html = Template("{% load ui %}{% level_badge d today %}").render(
        Context({"d": deadline, "today": today})
    )
    assert css in html and text in html


def test_level_badge_before_opening_shows_date_only() -> None:
    today = date(2026, 10, 16)
    deadline = deadlines.residence_expiry(date(2027, 5, 31))
    html = Template("{% load ui %}{% level_badge d today %}").render(
        Context({"d": deadline, "today": today})
    )
    assert "badge--not-yet" in html and "2027-05-31" in html


def test_level_badge_shows_legal_due_date_when_adjusted() -> None:
    deadline = deadlines.ad_hoc_filing(date(2026, 10, 3))  # 繰り延べ前 10/17（土）、法令上 10/19
    html = Template("{% load ui %}{% level_badge d today %}").render(
        Context({"d": deadline, "today": date(2026, 10, 16)})
    )
    assert 'title="2026-10-17 / 2026-10-19"' in html


def test_no_role_user_gets_styled_403() -> None:
    client = verified_client(make_user("norole"))
    assert client.get("/ui/").status_code == 403


STATIC_UI = Path(__file__).resolve().parents[2] / "apps" / "core" / "static" / "ui"


def role_variables(css_file: str) -> set[str]:
    """CSS ファイルで定義している役割の変数（セットごとの名前付きの色 --palette-* は除く）。"""
    text = (STATIC_UI / css_file).read_text()
    names = re.findall(r"^\s*(--[a-z0-9-]+)\s*:", text, re.MULTILINE)
    return {name for name in names if not name.startswith("--palette-")}


@pytest.mark.parametrize("key", ["b", "c", "d", "e"])
def test_design_sets_define_the_same_variables_as_tokens_css(key: str) -> None:
    # 採用したセットで tokens.css を置き換えても、変数の抜けで見た目が崩れない
    assert role_variables(f"tokens-{key}.css") == role_variables("tokens.css")


def test_components_use_only_variables_defined_in_tokens_css() -> None:
    used = set()
    for css_file in ("base.css", "components.css"):
        used |= set(re.findall(r"var\((--[a-z0-9-]+)\)", (STATIC_UI / css_file).read_text()))
    assert used <= role_variables("tokens.css")


def test_showcase_switches_design_set_by_query(admin_client: Client) -> None:
    default = admin_client.get("/ui/").content.decode()
    assert "tokens-" not in default
    assert 'data-design-set="a"' in default

    page = admin_client.get("/ui/?theme=d").content.decode()
    assert '<link rel="stylesheet" href="/static/ui/tokens-d.css">' in page
    assert 'href="?theme=d" aria-current="page"' in page

    unknown = admin_client.get("/ui/?theme=zzz").content.decode()
    assert 'data-design-set="a"' in unknown and "tokens-" not in unknown


def test_business_screens_do_not_use_design_sets(admin_client: Client) -> None:
    assert "tokens-" not in admin_client.get("/?theme=d").content.decode()


def test_showcase_sample_list_has_30_rows(admin_client: Client) -> None:
    context = admin_client.get("/ui/").context
    assert len(context["rows"]) == 30
    assert all(
        re.fullmatch(r"[A-Z]{2}\d{8}[A-Z]{2}", row["card"])
        for row in context["rows"]
        if row["card"]
    )
