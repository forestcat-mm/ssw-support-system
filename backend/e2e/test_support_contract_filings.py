"""通しのテスト（T28c）: 支援委託契約の終了を登録すると、特定技能1号の外国人ごとに第3-3-2号と
第3-2号の届出タスクがダッシュボードに出る。管理者が理由つきで終了を取り消すと、未着手のタスクは
取消になる。管理画面には 1 度も行かない。
"""

from collections.abc import Iterator
from datetime import date
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Request, expect

from apps.core.models import User
from apps.filings.models import Filing, FilingKind, FilingStatus
from apps.hosts.models import SupportContract
from e2e.conftest import SignIn
from tests.contracts.factories import make_contract, site_with_categories
from tests.workers.factories import add_residence, make_worker


@pytest.fixture
def admin_requests(page: Page) -> Iterator[list[str]]:
    seen: list[str] = []

    def watch(request: Request) -> None:
        if urlparse(request.url).path.startswith("/admin/"):
            seen.append(request.url)

    page.on("request", watch)
    yield seen


@pytest.fixture
def contract() -> SupportContract:
    site = site_with_categories()
    worker = make_worker(status="residing")
    add_residence(worker)
    make_contract(worker, site=site, ends_on=None)
    return SupportContract.objects.create(
        host_organization=site.host_organization,
        concluded_on=date(2025, 3, 1),
        starts_on=date(2025, 4, 1),
        ends_on=date(2027, 3, 31),
        monthly_fee=30000,
    )


@pytest.mark.parametrize("width", [1366, 390])
def test_terminate_the_support_contract_and_cancel(
    page: Page,
    sign_in: SignIn,
    support_staff: User,
    administrator: User,
    contract: SupportContract,
    admin_requests: list[str],
    width: int,
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    host = contract.host_organization
    sign_in(support_staff)
    base = page.url.rstrip("/")
    page.goto(f"{base}/hosts/{host.pk}/")
    page.locator(f'#support-contracts tr[data-contract="{contract.pk}"]').get_by_role(
        "link", name="終了を登録"
    ).click()
    page.locator("#id_terminated_on").fill("2026-09-30")
    page.locator("#id_termination_category").select_option("host")
    page.locator("#id_termination_reason").select_option("other")
    page.locator("#id_termination_note").fill("自社支援に切替")
    page.get_by_role("button", name="終了を登録").click()
    expect(page.get_by_text("届出タスクを 2 件起票しました（対象: 王芳）。")).to_be_visible()
    expect(page.locator("#support-contracts [data-terminated]")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth") <= width

    page.goto(f"{base}/")
    rows = page.locator("#dashboard-filings tbody tr")
    end_row = rows.filter(has_text="支援委託契約終了・締結届出")
    expect(end_row).to_contain_text("第3-3-2号の A 欄")
    expect(rows.filter(has_text="支援計画変更")).to_contain_text("第3-3-2号より前に提出")
    assert page.evaluate("document.documentElement.scrollWidth") <= width

    page.context.clear_cookies()
    sign_in(administrator)
    page.goto(f"{base}/hosts/{host.pk}/")
    page.locator("#support-contracts").get_by_role("link", name="終了を取り消す").click()
    page.locator("#id_reason").fill("別の契約に登録した")
    page.get_by_role("button", name="終了を取り消す").click()
    expect(page.get_by_text("終了を取り消しました。")).to_be_visible()
    statuses = set(
        Filing.objects.filter(
            kind__in=[
                FilingKind.SUPPORT_CONTRACT_END_OR_CONCLUSION,
                FilingKind.SUPPORT_PLAN_CHANGE,
            ]
        ).values_list("status", flat=True)
    )
    assert statuses == {FilingStatus.CANCELLED}
    assert admin_requests == []
