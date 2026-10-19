"""通しのテスト: 自動ログアウトの知らせと延長（修正 1 回目 J8、ADR-0030）。

時間は settings で短くする（本番の既定は無操作 60 分・ログインから 12 時間・5 分前に知らせる）。
無操作: 知らせが出て「延長する」で消え、また出て、時間が来ると「ログアウトしました」になり、
次の画面はログインの画面。ログインからの上限: 延長のボタンは出ず、時間が来るとログアウトする。
"""

from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, expect
from pytest_django.fixtures import Settings as SettingsWrapper

from apps.core.models import User
from e2e.conftest import SignIn


@pytest.mark.parametrize("width", [1366, 390])
def test_idle_notice_and_extension(
    page: Page, sign_in: SignIn, support_staff: User, settings: SettingsWrapper, width: int
) -> None:
    settings.SESSION_COOKIE_AGE = 16  # 無操作 16 秒で失効
    settings.SESSION_WARNING_SECONDS = 10  # 失効の 10 秒前に知らせる
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(support_staff)
    base = page.url.rstrip("/")
    page.goto(f"{base}/workers/")
    notice = page.locator("#session-notice")
    expect(notice).to_be_hidden()
    expect(notice).to_be_visible(timeout=12_000)
    expect(notice.locator('[data-session-state="idle"]')).to_contain_text(
        "まもなく自動でログアウト"
    )
    expect(notice.locator('[data-session-state="idle"]')).to_contain_text("あと 0 分")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    with page.expect_response(lambda response: "/accounts/keepalive/" in response.url) as sent:
        notice.get_by_role("button", name="延長する").click()
    assert sent.value.status == 204
    expect(notice).to_be_hidden()
    # 延ばした後も、操作が無ければまた知らせ、時間が来たらログアウトしたと出す（画面は移さない）
    expect(notice).to_be_visible(timeout=12_000)
    expect(notice.locator('[data-session-state="expired"]')).to_be_visible(timeout=15_000)
    expect(notice.get_by_role("button", name="延長する")).to_be_hidden()
    assert urlparse(page.url).path == "/workers/"
    page.reload()
    assert urlparse(page.url).path == "/accounts/login/"


@pytest.mark.parametrize("width", [1366, 390])
def test_absolute_limit_cannot_be_extended(
    page: Page, sign_in: SignIn, support_staff: User, settings: SettingsWrapper, width: int
) -> None:
    settings.SESSION_ABSOLUTE_TIMEOUT = 14  # ログインから 14 秒
    settings.SESSION_WARNING_SECONDS = 10
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(support_staff)
    base = page.url.rstrip("/")
    page.goto(f"{base}/workers/")
    notice = page.locator("#session-notice")
    expect(notice.locator('[data-session-state="absolute"]')).to_be_visible(timeout=12_000)
    expect(notice.get_by_role("button", name="延長する")).to_be_hidden()
    expect(notice.locator('[data-session-state="expired"]')).to_be_visible(timeout=15_000)
    page.goto(f"{base}/workers/")
    assert urlparse(page.url).path == "/accounts/login/"
    expect(page.get_by_text("ログアウトしました。もう一度ログインしてください。")).to_be_visible()
