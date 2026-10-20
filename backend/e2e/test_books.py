"""通しのテスト: 帳簿の出力（T57）。

外国人詳細の「帳簿」タブで ③④ を見て PDF をダウンロードし、設定の「帳簿」で機関全体の xlsx を
ダウンロードする。管理画面には行かない。
"""

import datetime
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Request, expect

from apps.contracts.models import EmploymentContract
from apps.core.models import User
from e2e.conftest import SignIn
from services import form_templates
from tests.forms_engine.test_form_application import make_case
from tests.workers.factories import add_residence


@pytest.fixture
def contract() -> EmploymentContract:
    form_templates.load_all()
    contract = make_case()
    add_residence(contract.worker, permitted_on=datetime.date(2026, 4, 1))
    return contract


@pytest.mark.parametrize("width", [1366, 390])
def test_books(
    page: Page, sign_in: SignIn, administrator: User, contract: EmploymentContract, width: int
) -> None:
    admin_requests: list[str] = []

    def watch(request: Request) -> None:
        if urlparse(request.url).path.startswith("/admin/"):
            admin_requests.append(request.url)

    page.on("request", watch)
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(administrator)
    base = page.url.rstrip("/")
    page.goto(f"{base}/workers/{contract.worker_id}/")
    page.get_by_role("link", name="帳簿", exact=True).click()
    expect(page.locator("#book-recipient")).to_contain_text("AB12345678CD")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    with page.expect_download() as pdf:
        page.locator("#book-pdf").click()
    assert pdf.value.suggested_filename.endswith(".pdf")
    page.goto(f"{base}/settings/books/")
    with page.expect_download() as xlsx:
        page.get_by_role("button", name="帳簿をダウンロード（Excel）").click()
    assert xlsx.value.suggested_filename.endswith(".xlsx")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    assert admin_requests == []
