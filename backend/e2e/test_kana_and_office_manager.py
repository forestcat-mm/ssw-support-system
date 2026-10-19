"""通しのテスト: 事務所の支援責任者を選ぶ（修正 1 回目 A5）と、外国人の氏名のカナ（A4）。

管理者が設定の事務所の画面で支援責任者を選んで保存する。担当者が外国人の編集でカナをひらがなで入れると
カタカナで保存され、一覧でひらがなで検索できる。管理画面には行かない。
"""

from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Request, expect

from apps.core.models import Office, User
from apps.workers.models import ForeignWorker
from e2e.conftest import SignIn
from tests.core.factories import make_office, make_staff
from tests.workers.factories import make_worker


@pytest.fixture
def office() -> Office:
    office = make_office(name="本社")
    make_staff(office=office, name="山田 一郎", is_support_manager=True)
    return office


@pytest.fixture
def worker() -> ForeignWorker:
    return make_worker(name_romaji="NGUYEN VAN AN", name_kanji="")


def watch_admin(page: Page) -> list[str]:
    seen: list[str] = []

    def watch(request: Request) -> None:
        if urlparse(request.url).path.startswith("/admin/"):
            seen.append(request.url)

    page.on("request", watch)
    return seen


@pytest.mark.parametrize("width", [1366, 390])
def test_choose_the_support_manager_of_the_office(
    page: Page, sign_in: SignIn, administrator: User, office: Office, width: int
) -> None:
    admin_requests = watch_admin(page)
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(administrator)
    page.goto(page.url.rstrip("/") + f"/settings/offices/{office.pk}/")
    page.locator("#id_support_manager").select_option(label="山田 一郎")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.get_by_role("button", name="保存する").click()
    office.refresh_from_db()
    assert office.support_manager is not None and office.support_manager.name == "山田 一郎"
    assert admin_requests == []


@pytest.mark.parametrize("width", [1366, 390])
def test_kana_in_hiragana_is_saved_in_katakana_and_searchable(
    page: Page, sign_in: SignIn, support_staff: User, worker: ForeignWorker, width: int
) -> None:
    admin_requests = watch_admin(page)
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(support_staff)
    base = page.url.rstrip("/")
    page.goto(f"{base}/workers/{worker.pk}/edit/")
    page.locator("#id_name_kana").fill("ぐえん　ゔぁん　あん")
    page.get_by_role("button", name="保存する").click()
    expect(page.locator("#worker-name-kana")).to_have_text("グエン　ヴァン　アン")
    page.goto(f"{base}/workers/?q=ぐえん&status=all")
    expect(page.locator("#worker-list tbody tr")).to_have_count(1)
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    assert admin_requests == []
