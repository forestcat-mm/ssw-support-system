"""通しのテスト: 外国人詳細（T13c）。一覧から開き、タブを切り替え、閲覧が監査ログに残る。"""

from datetime import timedelta

import pytest
from django.utils import timezone
from playwright.sync_api import Page, expect

from apps.core.models import AuditAction, AuditLog, User
from apps.filings.models import FilingKind, Trigger
from apps.workers.models import ForeignWorker
from e2e.conftest import SignIn
from services import filings
from tests.contracts.factories import make_contract
from tests.workers.factories import add_passport, add_residence, make_worker


@pytest.fixture
def worker() -> ForeignWorker:
    today = timezone.localdate()
    worker = make_worker()
    add_residence(worker, card_number="AB12345678CD", expires_on=today + timedelta(days=200))
    add_passport(worker, number="E12345678")
    contract = make_contract(worker)
    filings.create(
        FilingKind.CONTRACT_CHANGE, target=contract, basis_on=today, trigger=Trigger.MANUAL
    )
    return worker


def test_open_detail_from_list_and_switch_tabs(
    page: Page, sign_in: SignIn, support_staff: User, worker: ForeignWorker
) -> None:
    sign_in(support_staff)
    page.goto(page.url.rstrip("/") + "/workers/?status=all")
    page.get_by_role("link", name="王芳").click()

    expect(page.get_by_role("heading", level=1)).to_contain_text("王芳")
    expect(page.locator('[data-decrypted="card_number"]')).to_have_text("AB12345678CD")
    tabs = page.get_by_role("navigation", name="区分")

    tabs.get_by_role("link", name="契約").click()
    expect(page.locator("#contract-main")).to_contain_text("テスト工業株式会社")

    tabs.get_by_role("link", name="届出").click()
    expect(page.locator("#filings tbody tr")).to_have_count(1)
    expect(page.locator('#filings [data-level="within_14"]')).to_be_visible()

    tabs.get_by_role("link", name="履歴").click()
    expect(page.locator("#history")).to_contain_text("AB********CD")
    expect(page.locator("#history")).not_to_contain_text("AB12345678CD")

    page.get_by_role("link", name="← 一覧へ戻る").click()
    expect(page).to_have_url(page.url.split("/workers/")[0] + "/workers/?status=all")

    logs = AuditLog.objects.filter(
        action=AuditAction.VIEW, object_id=str(worker.pk), actor=support_staff
    )
    assert sorted(logs.values_list("context__tab", flat=True)) == [
        "basic",
        "contracts",
        "filings",
        "history",
    ]
    assert "AB12345678CD" not in str(list(logs.values_list("context", flat=True)))


def test_detail_fits_phone_width(
    page: Page, sign_in: SignIn, support_staff: User, worker: ForeignWorker
) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    sign_in(support_staff)
    base = page.url.rstrip("/")
    for suffix in ("", "contracts/", "filings/", "history/"):
        page.goto(f"{base}/workers/{worker.pk}/{suffix}")
        assert page.evaluate("document.documentElement.scrollWidth") <= 390, suffix
