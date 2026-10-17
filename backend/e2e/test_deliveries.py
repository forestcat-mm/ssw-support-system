"""通しのテスト: 飲食料品製造業分野の契約でキャリアアッププランの未交付が出る → 交付を記録 →
署名済みの控えを添付 → 取り消すと未交付に戻る（T22）。"""

from datetime import date

import pytest
from playwright.sync_api import Page, expect

from apps.core.models import User
from apps.workers.models import ForeignWorker
from e2e.conftest import SignIn
from tests.conftest import PDF_BYTES
from tests.contracts.factories import make_contract
from tests.core.factories import make_staff
from tests.hosts.factories import make_host, make_site
from tests.workers.factories import make_worker


@pytest.fixture
def worker() -> ForeignWorker:
    worker = make_worker(assigned_staff=make_staff(name="佐藤 花子", position="支援担当"))
    site = make_site(make_host(name="食品工業株式会社"), field_code="food_manufacturing")
    make_contract(worker, site=site, signed_on=date(2026, 3, 1))
    return worker


@pytest.mark.parametrize("width", [1366, 390])
def test_record_career_plan_attach_and_void(
    page: Page, sign_in: SignIn, support_staff: User, worker: ForeignWorker, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(support_staff)
    base = page.url.rstrip("/")
    page.goto(f"{base}/workers/{worker.pk}/support/records/")
    page.get_by_role("link", name="交付", exact=True).click()
    findings = page.locator("#career-plan-findings")
    expect(findings).to_contain_text("キャリアアッププランの交付の記録がありません")
    assert page.evaluate("document.documentElement.scrollWidth") <= width

    page.locator("#delivery-kinds").get_by_role("link", name="キャリアアッププラン").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("交付の記録: キャリアアッププラン")
    page.get_by_label("交付日").fill("2026-02-20")
    page.get_by_label("受入れ機関").check()
    page.get_by_label("内容を説明した").check()
    page.get_by_label("署名済みの控え・受領の記録（スキャン）").set_input_files(
        {"name": "控え.pdf", "mimeType": "application/pdf", "buffer": PDF_BYTES}
    )
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.get_by_role("button", name="保存する").click()

    expect(page.get_by_text("交付記録を保存しました。")).to_be_visible()
    expect(page.locator("#delivery-warnings")).to_have_count(0)
    expect(page.locator("#delivery-attachments")).to_contain_text("控え.pdf")
    assert page.evaluate("document.documentElement.scrollWidth") <= width

    page.get_by_role("link", name="交付の一覧へ").click()
    expect(page.locator("#career-plan-findings")).to_have_count(0)
    expect(page.locator("#deliveries [data-receipt]").first).to_have_text("あり")

    page.locator("#deliveries").get_by_role("link", name="取り消す").click()
    page.get_by_label("取り消しの理由").fill("別の人の記録")
    page.get_by_role("button", name="取り消す").click()
    expect(page.get_by_text("交付記録を取り消しました。")).to_be_visible()
    page.get_by_role("link", name="交付の一覧へ").click()
    expect(page.locator("#career-plan-findings")).to_contain_text(
        "キャリアアッププランの交付の記録がありません"
    )
