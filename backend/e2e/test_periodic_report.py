"""通しのテスト: 定期届出の第3-6号・別紙1・別紙2（T51）。

受入れ機関の詳細から定期届出の画面を開き、年度の対象者を確かめて 3 つの様式を作り、
第3-6号の記入済み（編集用）の xlsx をダウンロードする。管理画面には行かない。
"""

import datetime
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Request, expect

from apps.contracts.models import EmploymentContract
from apps.core.models import User
from apps.hosts.models import SupportContract
from e2e.conftest import SignIn
from services import form_templates
from tests.forms_engine.test_form_application import make_case
from tests.workers.factories import add_residence


@pytest.fixture
def contract() -> EmploymentContract:
    form_templates.load_all()
    contract = make_case()
    SupportContract.objects.filter(host_organization=contract.host_organization).update(
        ends_on=datetime.date(2027, 3, 31)
    )
    add_residence(contract.worker, permitted_on=datetime.date(2026, 4, 1))
    return contract


@pytest.mark.parametrize("width", [1366, 390])
def test_make_the_periodic_notification(
    page: Page,
    sign_in: SignIn,
    administrator: User,
    contract: EmploymentContract,
    width: int,
) -> None:
    admin_requests: list[str] = []

    def watch(request: Request) -> None:
        if urlparse(request.url).path.startswith("/admin/"):
            admin_requests.append(request.url)

    page.on("request", watch)
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(administrator)
    base = page.url.rstrip("/")
    page.goto(f"{base}/hosts/{contract.host_organization_id}/")
    page.locator("#periodic-report").get_by_role("link", name="定期届出の様式を作る").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("定期届出（第3-6号）")
    page.locator("#id_fiscal_year").fill("2026")
    page.get_by_role("button", name="検査する").click()
    expect(page.locator("#periodic-preview")).to_contain_text("期間中に支援した外国人 1 人")
    expect(page.locator(f'#periodic-lines [data-worker="{contract.worker_id}"]')).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.get_by_role("button", name="生成する").click()
    expect(page.get_by_text("定期届出の様式の生成を依頼しました。")).to_be_visible()
    expect(page.locator("#periodic-documents li")).to_have_count(3)
    page.locator("#periodic-documents li a", has_text="3-6 受入れ").click()
    expect(page.locator("#document-status")).to_have_text("生成しました。")
    with page.expect_download() as info:
        page.locator("#document-filled").click()
    assert info.value.suggested_filename.endswith(".xlsx")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    assert admin_requests == []
