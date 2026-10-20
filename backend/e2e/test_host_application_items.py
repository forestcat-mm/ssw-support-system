"""通しのテスト: 受入れ機関の一括申請の項目（T46b）。

受入れ機関の詳細の「一括申請の項目」から、決算と申告の新しい版を足す。申告で通常と違う答えには
内容が要る。管理画面には行かない。
"""

from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Request, expect

from apps.core.models import User
from apps.hosts.models import HostOrganization
from e2e.conftest import SignIn
from tests.hosts.factories import make_host


@pytest.fixture
def host() -> HostOrganization:
    return make_host(name="テスト工業株式会社")


def watch_admin(page: Page) -> list[str]:
    seen: list[str] = []

    def watch(request: Request) -> None:
        if urlparse(request.url).path.startswith("/admin/"):
            seen.append(request.url)

    page.on("request", watch)
    return seen


@pytest.mark.parametrize("width", [1366, 390])
def test_add_financial_results_and_a_declaration(
    page: Page, sign_in: SignIn, support_staff: User, host: HostOrganization, width: int
) -> None:
    admin_requests = watch_admin(page)
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(support_staff)
    base = page.url.rstrip("/")
    page.goto(f"{base}/hosts/{host.pk}/")
    section = page.locator("#host-application")
    expect(section).to_contain_text("一括申請の項目")
    assert page.evaluate("document.documentElement.scrollWidth") <= width

    section.get_by_role("link", name="追加").nth(0).click()
    expect(page.get_by_role("heading", level=1)).to_have_text("決算を追加")
    page.get_by_label("決算期末").fill("2026-03-31")
    page.get_by_label("売上高（円）").fill("300000000")
    page.get_by_label("経常損益（円）").fill("-1200000")
    page.get_by_role("button", name="保存する").click()
    expect(page.locator("#financial-years tbody tr")).to_have_count(1)

    page.locator("#declaration-create").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("申告の新しい版")
    page.get_by_label("申告日").fill("2026-10-01")
    page.locator('[data-item="answer_11"] input[value="yes"]').check()
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.get_by_role("button", name="保存する").click()
    expect(page.get_by_text("通常と違う答えのときは、内容を書いてください。")).to_be_visible()
    page.get_by_label("(11) の内容").fill("2025年に是正勧告を受け、是正済み。")
    page.get_by_role("button", name="保存する").click()
    expect(page.locator("#declaration-latest")).to_contain_text("2026-10-01")
    expect(page.locator("#declaration-unusual")).to_contain_text("(11)")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    assert admin_requests == []
