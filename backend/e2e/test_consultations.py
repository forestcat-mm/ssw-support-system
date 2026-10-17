"""通しのテスト: 相談を受け付ける → 経過を追記（対応中）→ 解決。経過が時系列で残る（T20）。"""

import pytest
from playwright.sync_api import Page, expect

from apps.core.models import User
from apps.workers.models import ForeignWorker
from e2e.conftest import SignIn
from tests.contracts.factories import make_contract, site_with_categories
from tests.core.factories import make_staff
from tests.workers.factories import make_worker


@pytest.fixture
def worker() -> ForeignWorker:
    worker = make_worker(assigned_staff=make_staff(name="佐藤 花子", position="支援担当"))
    make_contract(worker, site=site_with_categories())
    return worker


def add_entry(page: Page, status: str, body: str) -> None:
    page.get_by_role("link", name="経過を追記").click()
    page.get_by_label("追記後の対応状況").select_option(label=status)
    page.locator("#id_body").fill(body)
    page.get_by_role("button", name="追加", exact=True).click()
    expect(page.get_by_text("経過を追記しました。")).to_be_visible()


@pytest.mark.parametrize("width", [1366, 390])
def test_receive_progress_and_resolve(
    page: Page, sign_in: SignIn, support_staff: User, worker: ForeignWorker, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(support_staff)
    base = page.url.rstrip("/")
    page.goto(f"{base}/workers/{worker.pk}/support/records/")
    page.locator("#record-kinds").get_by_role("link", name="相談・苦情への対応").click()

    expect(page.get_by_role("heading", level=1)).to_have_text("相談の受付")
    page.get_by_label("主な分類").select_option(label="賃金")
    page.locator("#id_body").fill("残業代が少ないという相談。")
    page.get_by_role("button", name="保存する").click()
    expect(page.get_by_text("相談を受け付けました。")).to_be_visible()

    add_entry(page, "対応中", "受入れ機関に給与明細を確認した。")
    add_entry(page, "解決", "差額が支払われ、本人も了承した。")

    expect(page.locator("[data-consultation-status]")).to_have_text("解決")
    items = page.locator("#consultation-timeline > li")
    expect(items).to_have_count(3)
    expect(items.nth(0)).to_contain_text("残業代が少ないという相談。")
    expect(items.nth(1)).to_contain_text("受入れ機関に給与明細を確認した。")
    expect(items.nth(2)).to_contain_text("差額が支払われ")
    expect(items.nth(2).locator("[data-status]")).to_have_text("解決")
    assert page.evaluate("document.documentElement.scrollWidth") <= width

    # 一覧（既定は未解決）には出ず、「解決」で絞ると出る
    page.get_by_role("link", name="相談", exact=True).first.click()
    expect(page.get_by_text("条件に合う相談はありません。")).to_be_visible()
    page.get_by_label("対応状況").select_option(label="解決")
    page.get_by_role("button", name="絞り込む").click()
    expect(page.locator("#consultations tbody tr")).to_have_count(1)
