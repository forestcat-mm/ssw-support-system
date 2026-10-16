"""通しのテスト 1: ログイン → MFA → 業務画面のトップ（T13b で外国人一覧まで確かめる）。"""

import pytest
from playwright.sync_api import Page, expect

from apps.core.models import User
from e2e.conftest import SignIn


def test_sign_in_with_mfa_shows_business_screen(
    page: Page, sign_in: SignIn, support_staff: User
) -> None:
    sign_in(support_staff)
    expect(page.get_by_role("heading", level=1)).to_have_text("ダッシュボード")
    expect(page.get_by_role("navigation", name="メインメニュー")).to_be_visible()
    expect(page.get_by_role("button", name="ログアウト")).to_be_visible()
    # 支援担当者には管理画面・部品の見本が出ない
    expect(page.get_by_role("link", name="管理画面")).to_have_count(0)


def test_admin_sees_ui_components(page: Page, sign_in: SignIn, administrator: User) -> None:
    sign_in(administrator)
    page.get_by_role("link", name="部品の見本").first.click()
    expect(page.get_by_role("heading", level=1)).to_have_text("部品の見本")
    expect(page.locator('[data-level="overdue"]').first).to_be_visible()


# 比較セットごとの主要ボタンの塗り（tokens.css / tokens-b.css〜tokens-e.css）
PRIMARY_BUTTON = {
    "a": "rgb(0, 138, 232)",
    "b": "rgb(0, 0, 0)",
    "c": "rgb(0, 64, 56)",
    "d": "rgb(12, 23, 84)",
    "e": "rgb(0, 0, 0)",
}


@pytest.mark.parametrize("theme", sorted(PRIMARY_BUTTON))
def test_design_sets_switch_and_fit_phone_width(
    page: Page, sign_in: SignIn, administrator: User, theme: str
) -> None:
    # 第9章「レスポンシブ」: スマートフォンの幅でページが横にはみ出さない（表は表の中で送る）
    page.set_viewport_size({"width": 390, "height": 844})
    sign_in(administrator)
    page.goto(page.url.rstrip("/") + f"/ui/?theme={theme}")
    expect(page.get_by_role("heading", level=1)).to_have_text("部品の見本")
    expect(page.locator("#sample-list .button--primary")).to_have_css(
        "background-color", PRIMARY_BUTTON[theme]
    )
    expect(page.locator('[data-token="--color-canvas"]')).not_to_be_empty()
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
