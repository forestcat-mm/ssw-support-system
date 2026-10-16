"""通しのテスト: ログイン → ダッシュボード → 超過で絞る → 行を開いて「対応中」にする（T14）。"""

from datetime import timedelta

import pytest
from django.utils import timezone
from playwright.sync_api import Page, expect

from apps.core.models import User
from apps.filings.models import Filing, FilingKind, Trigger
from e2e.conftest import SignIn
from services import filings
from tests.contracts.factories import make_contract, site_with_categories
from tests.workers.factories import make_worker


@pytest.fixture
def overdue_task() -> Filing:
    contract = make_contract(make_worker(), site=site_with_categories())
    filings.create(
        FilingKind.CONTRACT_TERMINATION,
        target=contract,
        basis_on=timezone.localdate() - timedelta(days=3),
        trigger=Trigger.MANUAL,
    )
    filing, _created = filings.create(
        FilingKind.CONTRACT_CHANGE,
        target=contract,
        basis_on=timezone.localdate() - timedelta(days=20),  # 14 日を過ぎて超過
        trigger=Trigger.MANUAL,
    )
    return filing


@pytest.mark.parametrize("width", [1366, 390])
def test_filter_overdue_and_start_the_task(
    page: Page, sign_in: SignIn, support_staff: User, overdue_task: Filing, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(support_staff)
    expect(page.get_by_role("heading", level=1)).to_have_text("ダッシュボード")
    rows = page.locator("#dashboard-filings tbody tr")
    expect(rows).to_have_count(2)

    page.locator('[data-level-count="overdue"]').click()
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("雇用契約変更届出")
    expect(rows.first.locator('[data-level="overdue"]')).to_be_visible()

    rows.first.get_by_role("link", name="雇用契約変更届出").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("雇用契約変更届出")
    page.locator("#transition").get_by_label("変更後の状態").select_option(label="対応中")
    page.locator("#transition").get_by_role("button", name="状態を変更する").click()

    # 同じ条件（超過）のダッシュボードへ戻り、状態が変わっている
    expect(page.get_by_text("状態を「対応中」にしました。")).to_be_visible()
    assert "level=overdue" in page.url
    row = page.locator(f'#dashboard-filings tr[data-filing="{overdue_task.pk}"]')
    expect(row).to_contain_text("対応中")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
