"""通しのテスト: 一括届出（電子届出の一括申込用ツール。T50）。

一括届出を作り、届出タスクを入れ、行の届出の内容を直して保存し、検査が通ったら Excel を作って
ダウンロードする。管理画面には行かない。
"""

import datetime
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Request, expect

from apps.contracts.models import EmploymentContract
from apps.core.models import User
from apps.filings.models import FilingKind, Trigger
from e2e.conftest import SignIn
from services import filings, form_templates
from tests.forms_engine.test_form_application import make_case
from tests.workers.factories import add_residence


@pytest.fixture
def contract() -> EmploymentContract:
    form_templates.load_all()
    contract = make_case()
    add_residence(contract.worker, permitted_on=datetime.date(2025, 10, 1))
    filings.create(
        FilingKind.MISSING,
        target=contract.worker,
        basis_on=datetime.date(2026, 10, 1),
        trigger=Trigger.MANUAL,
    )
    return contract


def watch_admin(page: Page) -> list[str]:
    seen: list[str] = []

    def watch(request: Request) -> None:
        if urlparse(request.url).path.startswith("/admin/"):
            seen.append(request.url)

    page.on("request", watch)
    return seen


@pytest.mark.parametrize("width", [1366, 390])
def test_create_fill_in_and_generate(
    page: Page,
    sign_in: SignIn,
    administrator: User,
    contract: EmploymentContract,
    width: int,
) -> None:
    admin_requests = watch_admin(page)
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(administrator)
    base = page.url.rstrip("/")
    page.goto(f"{base}/forms/efile/")
    page.locator("#efile-create").click()
    page.locator("select[name=form_id]").select_option("efile-3-4")
    page.locator("select[name=host]").select_option(str(contract.host_organization_id))
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.get_by_role("button", name="一括届出を作る").click()
    expect(page.get_by_role("heading", level=1)).to_contain_text("第3-4号")

    page.locator("#efile-add-form summary").click()
    page.locator('#efile-add-form input[type="checkbox"]').first.check()
    page.locator("#efile-add-form").get_by_role("button", name="追加").click()
    rows = page.locator("#efile-rows tbody tr")
    expect(rows).to_have_count(1)
    # 行方不明の届出タスクから、事由・区分・現状が入っている
    expect(page.locator('[data-problems="0"]')).to_have_count(1)
    assert page.evaluate("document.documentElement.scrollWidth") <= width

    rows.first.get_by_role("link", name="入力").click()
    expect(page.get_by_role("heading", level=1)).to_contain_text("届出の内容")
    expect(page.get_by_label("事由の区分", exact=True)).to_have_value("行方不明")
    page.get_by_label("事案の概要").fill("連絡が取れない")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.get_by_role("button", name="保存する").click()
    expect(page.get_by_text("保存しました")).to_be_visible()
    expect(page.locator('[data-problems="0"]')).to_have_count(1)

    page.get_by_role("button", name="Excel を作る").click()
    expect(page.get_by_text("1 個のファイルを作りました。")).to_be_visible()
    with page.expect_download() as download:
        page.locator("#files a.attachment-list__name").first.click()
    assert download.value.suggested_filename.endswith(".xlsm")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    assert admin_requests == []
