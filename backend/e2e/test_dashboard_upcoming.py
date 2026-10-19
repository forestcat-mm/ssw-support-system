"""通しのテスト: ダッシュボードの「担当者なし」と「今後の予定」（修正 1 回目 A3）。

担当者（自分の担当で開く）に、受入れ機関の支援委託契約の更新（担当者なし）のタスクと、まだタスクに
なっていない加入の更新の期限が出る。管理画面には行かない。
"""

from datetime import date, timedelta
from urllib.parse import urlparse

import pytest
from django.utils import timezone
from playwright.sync_api import Page, Request, expect

from apps.core.models import User
from apps.hosts.models import HostMembership, SupportContract
from e2e.conftest import SignIn
from services import deadlines, filings
from tests.core.factories import make_staff
from tests.hosts.factories import field, make_host


@pytest.fixture
def host_deadlines(support_staff: User) -> None:
    make_staff(name="佐藤 花子", user=support_staff)
    today = timezone.localdate()
    host = make_host()
    HostMembership.objects.create(
        host_organization=host,
        field=field("industrial_products"),
        joined_on=date(2025, 1, 1),
        valid_until=deadlines.add_months(today, 4),  # 受付は 3 か月先から → 今後の予定
    )
    SupportContract.objects.create(
        host_organization=host,
        concluded_on=date(2025, 3, 1),
        starts_on=date(2025, 4, 1),
        ends_on=today + timedelta(days=60),  # 受付は過ぎている → タスク（担当者なし）
    )
    filings.create_scheduled(today)


@pytest.mark.usefixtures("host_deadlines")
@pytest.mark.parametrize("width", [1366, 390])
def test_unassigned_and_upcoming_sections(
    page: Page, sign_in: SignIn, support_staff: User, width: int
) -> None:
    admin_requests: list[str] = []

    def watch(request: Request) -> None:
        if urlparse(request.url).path.startswith("/admin/"):
            admin_requests.append(request.url)

    page.on("request", watch)
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(support_staff)
    unassigned = page.locator("#dashboard-unassigned")
    expect(unassigned).to_contain_text("支援委託契約の更新")
    upcoming = page.locator("#dashboard-upcoming")
    upcoming.locator("summary").click()
    expect(upcoming.locator('tr[data-kind="membership_renewal"]')).to_contain_text(
        "まだタスクではない"
    )
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    assert admin_requests == []
