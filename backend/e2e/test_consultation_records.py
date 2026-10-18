"""通しのテスト: 受入れ機関の詳細から、期間の相談の相談記録書（第5-4号）を作る（T35、ADR-0024）。

受入れ機関の詳細 →「相談記録書を作る」→ 件数を確かめる → 生成 → 受入れ機関の詳細の一覧に出る →
生成物の画面で PDF をダウンロード。管理画面には 1 度も行かない。
"""

from datetime import date
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Request, expect

from apps.core.models import User
from apps.hosts.models import HostOrganization
from apps.support.models import ConsultationStatus
from apps.workers.models import WorkerStatus
from e2e.conftest import SignIn
from services import consultations, form_templates
from services.consultations import EntryInput, ReceptionInput
from services.support_catalog import SupportItem
from services.support_records import RecordInput
from tests.contracts.factories import make_contract, site_with_categories
from tests.core.factories import make_organization, make_staff
from tests.workers.factories import make_worker


@pytest.fixture
def host() -> HostOrganization:
    form_templates.load_all()
    make_organization(name="（ダミー）サンプル登録支援機関")
    worker = make_worker(assigned_staff=make_staff(name="佐藤 花子"), status=WorkerStatus.RESIDING)
    contract = make_contract(worker, site=site_with_categories())  # 2026-04-01〜
    on = date(2026, 6, 1)
    case = consultations.create_consultation(
        worker,
        ReceptionInput(
            record=RecordInput(
                kind=SupportItem.CONSULTATION,
                occurred_on=on,
                method="phone",
                staff=worker.assigned_staff,
                body="寮の騒音の相談。",
            ),
            category="daily_life",
        ),
        by=None,
        today=date(2026, 10, 17),
    )
    consultations.add_entry(
        case,
        EntryInput(
            occurred_on=on,
            method="phone",
            staff=worker.assigned_staff,
            body="管理会社に連絡した。",
            status_after=ConsultationStatus.RESOLVED,
        ),
        by=None,
        today=date(2026, 10, 17),
    )
    return contract.host_organization


@pytest.mark.parametrize("width", [1366, 390])
def test_generate_consultation_records_from_the_host(
    page: Page, sign_in: SignIn, support_staff: User, host: HostOrganization, width: int
) -> None:
    admin_requests: list[str] = []

    def watch(request: Request) -> None:
        if urlparse(request.url).path.startswith("/admin/"):
            admin_requests.append(request.url)

    page.on("request", watch)
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(support_staff)
    base = page.url.rstrip("/")
    page.goto(f"{base}/hosts/{host.pk}/")
    page.locator("#consultation-records").get_by_role("link", name="相談記録書を作る").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("相談記録書を作る（第5-4号）")
    page.locator("#id_starts_on").fill("2026-04-01")
    page.locator("#id_ends_on").fill("2027-03-31")
    page.get_by_role("button", name="検査する").click()
    expect(page.locator("#consultation-record-preview")).to_contain_text("期間の相談は 1 件です")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.get_by_role("button", name="生成する").click()
    expect(page.get_by_text("相談記録書の生成を依頼しました。")).to_be_visible()
    page.locator("#consultation-records li a").first.click()
    expect(page.locator("#document-status")).to_have_text("生成しました。")
    with page.expect_download() as info:
        page.locator("#document-pdf").click()
    assert info.value.suggested_filename.startswith("5-4_")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    assert admin_requests == []
