"""通しのテスト: 相談の経過に通報を追記すると第4-3号（特異事案報告）のタスクが期限つきで起票され、
相談を取り消すとタスクも取消になる（T24a）。"""

from datetime import timedelta

import pytest
from django.utils import timezone
from playwright.sync_api import Page, expect

from apps.core.models import User
from apps.workers.models import ForeignWorker
from e2e.conftest import SignIn, menu_action
from services import deadlines
from tests.contracts.factories import make_contract, site_with_categories
from tests.core.factories import make_staff
from tests.workers.factories import make_worker


@pytest.fixture
def worker() -> ForeignWorker:
    worker = make_worker(assigned_staff=make_staff(name="佐藤 花子", position="支援担当"))
    make_contract(worker, site=site_with_categories())
    return worker


@pytest.mark.parametrize("width", [1366, 390])
def test_report_raises_and_void_cancels_the_form_4_3_task(
    page: Page, sign_in: SignIn, support_staff: User, worker: ForeignWorker, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(support_staff)
    base = page.url.rstrip("/")
    page.goto(f"{base}/workers/{worker.pk}/support/records/")
    page.locator("#record-kinds").get_by_role("link", name="相談・苦情への対応").click()
    page.get_by_label("主な分類").select_option(label="賃金")
    page.locator("#id_body").fill("残業代が支払われていないという相談。")
    page.get_by_role("button", name="保存する").click()
    expect(page.get_by_text("相談を受け付けました。")).to_be_visible()

    reported_on = timezone.localdate() - timedelta(days=1)
    page.get_by_role("link", name="経過を追記").click()
    page.locator("#id_body").fill("労働基準監督署に通報した。")
    page.get_by_label("通報", exact=True).check()
    page.get_by_label("通報先", exact=True).select_option(label="労働基準監督署")
    page.locator("#id_reported_on").fill(reported_on.isoformat())
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.get_by_role("button", name="追加", exact=True).click()
    due = deadlines.ad_hoc_filing(reported_on).due_on
    expect(
        page.get_by_text(f"第4-3号（特異事案報告）のタスク（期限 {due.isoformat()}）")
    ).to_be_visible()

    page.goto(f"{base}/workers/{worker.pk}/filings/")
    task = page.locator("#filings tbody tr", has_text="特異事案報告")
    expect(task).to_have_attribute("data-status", "not_started")
    assert page.evaluate("document.documentElement.scrollWidth") <= width

    page.goto(f"{base}/workers/{worker.pk}/support/records/")
    page.locator("#support-records").get_by_role("link", name="相談").first.click()
    menu_action(page, "取り消す")
    page.get_by_label("取り消しの理由").fill("別の人の相談")
    page.get_by_role("button", name="取り消す").click()
    expect(
        page.get_by_text("この記録から起票した第4-3号（特異事案報告）のタスクを取り消しました。")
    ).to_be_visible()

    page.goto(f"{base}/workers/{worker.pk}/filings/")
    expect(
        page.locator('#filings tbody tr[data-status="not_started"]', has_text="特異事案報告")
    ).to_have_count(0)
