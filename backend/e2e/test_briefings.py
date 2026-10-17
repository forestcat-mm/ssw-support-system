"""通しのテスト: 生活オリエンテーションを 2 日に分けて記録する → 所要時間の不足と
未チェックが警告に出る → 署名済みの確認書を添付すると、署名の警告が消える（T21）。"""

import pytest
from playwright.sync_api import Page, expect

from apps.core.models import User
from apps.workers.models import ForeignWorker
from e2e.conftest import SignIn
from tests.conftest import PDF_BYTES
from tests.contracts.factories import make_contract, site_with_categories
from tests.core.factories import make_staff
from tests.workers.factories import make_worker


@pytest.fixture
def worker() -> ForeignWorker:
    worker = make_worker(assigned_staff=make_staff(name="佐藤 花子", position="支援担当"))
    make_contract(worker, site=site_with_categories())
    return worker


def fill_session(page: Page, index: int, day: str, start: str, end: str) -> None:
    page.locator(f"#id_sessions-{index}-occurred_on").fill(day)
    page.locator(f"#id_sessions-{index}-start_time").fill(start)
    page.locator(f"#id_sessions-{index}-end_time").fill(end)


@pytest.mark.parametrize("width", [1366, 390])
def test_record_orientation_with_warnings_and_sign(
    page: Page, sign_in: SignIn, support_staff: User, worker: ForeignWorker, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(support_staff)
    base = page.url.rstrip("/")
    page.goto(f"{base}/workers/{worker.pk}/support/records/")
    page.locator("#record-kinds").get_by_role("link", name="生活オリエンテーション").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("記録: 生活オリエンテーション")

    page.get_by_label("場所").fill("受入れ機関の会議室")
    fill_session(page, 0, "2026-10-01", "09:00", "12:00")
    page.get_by_role("button", name="実施日時を足す").click()
    fill_session(page, 1, "2026-10-02", "09:00", "12:00")

    # 細目はすべて「説明した」にし、1 つを「該当なし」、1 つを未チェックのまま残す
    page.locator('#briefing-checklist input[type="radio"][value="done"]').evaluate_all(
        "radios => radios.forEach(radio => { radio.checked = true; })"
    )
    item = page.locator('[data-item="4.a.9"]')
    item.get_by_role("radio", name="該当なし").check()
    item.get_by_role("textbox", name="該当なしの理由（4.a.9）").fill("本人に子どもがいない")
    page.locator('[data-item="4.e.4"]').get_by_role("radio", name="未チェック").check()
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.get_by_role("button", name="保存する").click()

    expect(page.get_by_text("支援実績を保存しました。")).to_be_visible()
    warnings = page.locator("#briefing-warnings")
    expect(warnings).to_contain_text("必要な 8 時間以上を満たしていません。")
    expect(warnings).to_contain_text("未チェックの必須項目が 1 件あります。")
    expect(warnings).to_contain_text("署名済みの確認書が添付されていません。")
    expect(page.locator("#briefing-counts")).to_have_text("説明した 27・該当なし 1・未チェック 1")
    expect(page.locator('[data-section="4.a"]')).to_have_attribute("data-complete", "true")
    expect(page.locator('[data-section="4.e"]')).to_have_attribute("data-complete", "false")
    assert page.evaluate("document.documentElement.scrollWidth") <= width

    page.get_by_role("link", name="署名済みの確認書を添付").click()
    page.get_by_label("署名日").fill("2026-10-02")
    page.get_by_label("署名済みの確認書（スキャン）").set_input_files(
        {"name": "確認書.pdf", "mimeType": "application/pdf", "buffer": PDF_BYTES}
    )
    page.get_by_role("button", name="追加", exact=True).click()
    expect(page.get_by_text("署名済みの確認書を添付しました。")).to_be_visible()
    expect(page.locator("#briefing-warnings")).not_to_contain_text(
        "署名済みの確認書が添付されていません"
    )
    expect(page.locator("#record-attachments")).to_contain_text("確認書.pdf")
