"""通しのテスト: 生活オリエンテーションの記録から確認書（第5-8号）を生成する（T32、ADR-0021）。

記録の画面の「確認書を作る」→ 不足なし → 生成 → 生成物の画面で PDF をダウンロード → 記録へ戻る。
"""

from datetime import date
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Request, expect

from apps.core.models import User
from apps.hosts.models import SupportContract
from apps.support.models import Briefing
from e2e.conftest import SignIn
from services import briefings, form_templates
from tests.contracts.factories import make_contract, site_with_categories
from tests.core.factories import make_organization, make_staff
from tests.support.test_briefings import orientation
from tests.workers.factories import make_worker


@pytest.fixture
def briefing() -> Briefing:
    form_templates.load_all()
    make_organization(name="（ダミー）サンプル登録支援機関")
    worker = make_worker(assigned_staff=make_staff(name="佐藤 花子", position="支援担当"))
    contract = make_contract(worker, site=site_with_categories())
    SupportContract.objects.create(
        host_organization=contract.host_organization,
        concluded_on=date(2026, 1, 1),
        starts_on=date(2026, 1, 1),
    )
    return briefings.create_briefing(worker, orientation(worker), by=None, today=date(2026, 10, 17))


@pytest.mark.parametrize("width", [1366, 390])
def test_generate_the_orientation_confirmation(
    page: Page, sign_in: SignIn, support_staff: User, briefing: Briefing, width: int
) -> None:
    admin_requests: list[str] = []

    def watch(request: Request) -> None:
        if urlparse(request.url).path.startswith("/admin/"):
            admin_requests.append(request.url)

    page.on("request", watch)
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(support_staff)
    base = page.url.rstrip("/")
    page.goto(f"{base}/workers/{briefing.worker_id}/support/briefings/{briefing.pk}/")
    page.locator("#briefing-generate").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("確認書を作る（第5-8号）")
    expect(page.locator("#generation-findings")).to_contain_text("不足はありません")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.get_by_role("button", name="生成する").click()

    expect(page.locator("#document-status")).to_have_text("生成しました。")
    with page.expect_download() as info:
        page.locator("#document-pdf").click()
    assert info.value.suggested_filename.startswith("5-8_")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.get_by_role("link", name=str(briefing.worker)).first.click()
    expect(page.locator("#briefing")).to_be_visible()
    assert admin_requests == []
