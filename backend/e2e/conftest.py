"""通しのテスト（Playwright）の共通部分。make e2e（e2e コンテナ）で動かす。

Django はテスト用の DB で live_server として同じプロセスに起動する。Playwright の同期 API は
イベントループを使うため DJANGO_ALLOW_ASYNC_UNSAFE=true にしている（Dockerfile.e2e）。
"""

import urllib.error
import urllib.request
from collections.abc import Callable

import pytest
from django.core.management import call_command
from django.urls import get_resolver, reverse
from playwright.sync_api import Page
from pytest_django.live_server_helper import LiveServer

from apps.core.models import User
from tests.core.factories import TEST_PASSWORD, make_totp_device, make_user, totp_token

SignIn = Callable[[User], None]


STARTUP_TIMEOUT = 15  # 秒。起動の確かめの 1 回の上限


def pytest_sessionstart(session: pytest.Session) -> None:
    """アプリ（URL の設定とすべてのビュー）を読み込めないなら、テストを始めずに止める。

    e2e のイメージに依存が足りない（例: docxtpl）と、画面がすべて 500 になり、テストが 1 件ずつ
    画面の時間切れを待つ（T29 で 24 分かかった）。読み込みの失敗をここで先に知らせる。
    """
    try:
        get_resolver().url_patterns  # noqa: B018  URL の設定から、すべてのビューを import する
        call_command("check", fail_level="ERROR", verbosity=0)
    except Exception as error:  # noqa: BLE001  原因を出して止める
        pytest.exit(
            f"the application failed to load ({type(error).__name__}: {error}). "
            "If a dependency is missing, rebuild the e2e image: "
            "docker compose --profile e2e build e2e",
            returncode=3,
        )


@pytest.fixture(scope="session", autouse=True)
def live_server_responds(live_server: LiveServer) -> None:
    """テスト用のサーバーが応答する（500 や時間切れなら、残りを流さずに止める）。"""
    try:
        with urllib.request.urlopen(
            live_server.url + reverse("accounts:login"), timeout=STARTUP_TIMEOUT
        ):
            pass
    except urllib.error.HTTPError as error:
        if error.code >= 500:
            pytest.exit(f"the test server returned {error.code} at startup", returncode=3)
    except OSError as error:
        pytest.exit(f"the test server did not respond at startup ({error})", returncode=3)


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


def answer_all_no(page: Page) -> None:
    """定期面談の画面で、報告書の質問を 2 行とも「問題無」にし、⑥ を「なし」にする（T35）。"""
    for scope in ("#interview-self-answers", "#interview-supervisor-answers"):
        page.locator(scope).get_by_role("button", name="すべて「問題無」にする").click()
    page.locator('input[name="nonconformity-exists"][value="no"]').check()
