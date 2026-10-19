"""通しのテスト: 登録の画面の任意の区画と入国前の注記（修正 1 回目 B5）、日付の無い連絡先（B6）。"""

from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Request, expect

from apps.core.models import User
from apps.workers.models import ForeignWorker
from e2e.conftest import SignIn
from tests.workers.factories import make_worker


@pytest.fixture
def worker() -> ForeignWorker:
    return make_worker()


@pytest.mark.parametrize("width", [1366, 390])
def test_optional_sections_and_undated_contact(
    page: Page, sign_in: SignIn, support_staff: User, worker: ForeignWorker, width: int
) -> None:
    admin_requests: list[str] = []

    def watch(request: Request) -> None:
        if urlparse(request.url).path.startswith("/admin/"):
            admin_requests.append(request.url)

    page.on("request", watch)
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(support_staff)
    base = page.url.rstrip("/")
    page.goto(f"{base}/workers/new/")
    page.locator("#section-residence summary").click()
    expect(page.locator("#section-residence")).to_contain_text("入れる場合は必須")
    expect(page.locator("#pre-entry-note")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth") <= width

    # 本国の連絡先を、適用開始日なしで足す（適用開始日は最後の欄）
    page.goto(f"{base}/workers/{worker.pk}/contact/new/")
    page.locator("#id_home_country_address").fill("Sample City 1")
    page.get_by_role("button", name="保存する").click()
    expect(page.locator("#contact-effective-from")).to_contain_text("適用開始日なし")
    assert admin_requests == []
