"""通しのテスト（Playwright）の共通部分。make e2e（e2e コンテナ）で動かす。

Django はテスト用の DB で live_server として同じプロセスに起動する。Playwright の同期 API は
イベントループを使うため DJANGO_ALLOW_ASYNC_UNSAFE=true にしている（Dockerfile.e2e）。
"""

from collections.abc import Callable

import pytest
from playwright.sync_api import Page
from pytest_django.live_server_helper import LiveServer

from apps.core.models import User
from tests.core.factories import TEST_PASSWORD, make_totp_device, make_user, totp_token

SignIn = Callable[[User], None]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """live_server は DB を空にする方式（transaction=True）で動くため、マイグレーションで入れた
    初期データ（ロール・国・分野・業務区分など）を serialized_rollback でテストごとに戻す。"""
    for item in items:
        item.add_marker(pytest.mark.django_db(transaction=True, serialized_rollback=True))


@pytest.fixture
def sign_in(page: Page, live_server: LiveServer) -> SignIn:
    """ログイン画面からパスワード → TOTP の順にログインする。"""

    def run(user: User) -> None:
        device = make_totp_device(user)
        page.goto(f"{live_server.url}/")
        page.get_by_label("ユーザー名").fill(user.get_username())
        page.get_by_label("パスワード").fill(TEST_PASSWORD)
        page.get_by_role("button", name="次へ").click()
        page.get_by_label("6 桁のコード または 回復用コード").fill(totp_token(device))
        page.get_by_role("button", name="ログイン").click()

    return run


@pytest.fixture
def support_staff() -> User:
    return make_user("tanto", roles=("support_staff",))


@pytest.fixture
def administrator() -> User:
    return make_user("kanri", roles=("admin",))
