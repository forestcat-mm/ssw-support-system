"""通しのテスト: ログイン画面の「パスワードを忘れた場合」→ 申請 → メールのリンク →
新しいパスワード → 新しいパスワードと認証アプリでログインできる（T25）。
メールはテストの送信箱（mail.outbox）で読む。"""

import re

import pytest
from django.core import mail
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from apps.core.models import User
from tests.core.factories import make_totp_device, make_user, totp_token

NEW_PASSWORD = "kN8#vQ2!pLm4"
LINK = re.compile(r"https?://[^/\s]+(/accounts/password-reset/[^/\s]+/)")


@pytest.fixture
def user() -> User:
    return make_user("tanto", roles=("support_staff",), email="tanto@example.invalid")


@pytest.mark.parametrize("width", [1366, 390])
def test_reset_the_password_and_sign_in(
    page: Page, live_server: LiveServer, user: User, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    device = make_totp_device(user)
    page.goto(f"{live_server.url}/accounts/login/")
    page.get_by_role("link", name="パスワードを忘れた場合").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("パスワードの再設定")
    page.get_by_label("メールアドレス").fill("tanto@example.invalid")
    page.get_by_role("button", name="リンクを送る").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("メールを確認してください")
    assert page.evaluate("document.documentElement.scrollWidth") <= width

    [message] = mail.outbox
    match = LINK.search(str(message.body))
    assert match is not None
    page.goto(f"{live_server.url}{match.group(1)}")
    expect(page).to_have_url(f"{live_server.url}/accounts/password-reset/set/")  # トークンを外す
    page.locator("#id_new_password1").fill(NEW_PASSWORD)
    page.locator("#id_new_password2").fill(NEW_PASSWORD)
    page.get_by_role("button", name="設定する").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("パスワードを変更しました")
    assert "パスワードが変更されました" in mail.outbox[-1].subject  # 本人への通知

    page.get_by_role("link", name="ログイン").click()
    page.get_by_label("ユーザー名").fill("tanto")
    page.get_by_label("パスワード").fill(NEW_PASSWORD)
    page.get_by_role("button", name="次へ").click()
    page.get_by_label("6 桁のコード または 回復用コード").fill(totp_token(device))
    page.get_by_role("button", name="ログイン").click()
    expect(page).to_have_url(f"{live_server.url}/")
