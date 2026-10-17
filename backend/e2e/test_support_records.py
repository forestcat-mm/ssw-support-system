"""通しのテスト: 支援実績（T18）。1 件記録する → 一覧に出る → 取り消す → 打ち消し線で残る。"""

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


@pytest.mark.parametrize("width", [1366, 390])
def test_record_then_void_and_see_strikethrough(
    page: Page, sign_in: SignIn, support_staff: User, worker: ForeignWorker, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(support_staff)
    base = page.url.rstrip("/")
    page.goto(f"{base}/workers/{worker.pk}/support/")
    page.get_by_role("link", name="記録", exact=True).click()
    expect(page.get_by_text("まだ記録はありません。")).to_be_visible()

    # 種別のボタンから入力画面を開く（担当者・本文の言語は補われている）
    page.locator("#record-kinds").get_by_role("link", name="日本人との交流促進").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("記録: 日本人との交流促進")
    expect(page.locator("#record-form")).to_have_attribute("data-keepalive", "/accounts/keepalive/")
    page.get_by_label("行事・活動").fill("地域の夏祭り")
    page.locator("#id_body").fill("参加の手続を補助し、当日同行した。")
    page.get_by_role("button", name="保存する").click()
    expect(page.get_by_text("支援実績を保存しました。")).to_be_visible()
    expect(page.locator("#record-fields")).to_contain_text("佐藤 花子（支援担当）")
    expect(page.locator("#record-fields")).to_contain_text("地域の夏祭り")

    # 一覧に出る → 取り消す（理由は必須）
    page.get_by_role("link", name="記録の一覧へ").click()
    row = page.locator("#support-records tbody tr")
    expect(row).to_have_count(1)
    expect(row).to_contain_text("地域の夏祭り")
    row.get_by_role("link", name="取り消す").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("支援実績の取消")
    page.get_by_role("button", name="取り消す").click()
    expect(page.locator("#id_reason-error")).to_be_visible()
    page.get_by_label("取り消しの理由").fill("別の人の記録")
    page.get_by_role("button", name="取り消す").click()

    expect(page.get_by_text("支援実績を取り消しました。")).to_be_visible()
    expect(row).to_have_class("is-voided")
    expect(row).to_contain_text("別の人の記録")
    decoration = row.locator("td").first.evaluate("e => getComputedStyle(e).textDecorationLine")
    assert decoration == "line-through"
    assert page.evaluate("document.documentElement.scrollWidth") <= width
