"""通しのテスト: 受入れ機関の書類の保管庫と提出書類一覧表（T42 の追加）。

受入れ機関の詳細 →「書類を追加」→ 年間カレンダー（事業所・対象期間・日本語と中国語）を保管 →
申請の書類の一覧で年間カレンダーの行が「受入れ機関の書類から」→「提出用の PDF を作る」→
提出書類一覧表を作る印と行ごとの提出確認欄 → 作る → 中身の先頭が一覧表。管理画面には行かない。
"""

import io
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, expect
from pypdf import PdfWriter

from apps.contracts.models import EmploymentContract, VariableHoursUnit
from apps.core.models import User
from apps.forms_engine.models import SubmissionBundle
from apps.hosts.models import HostDocument
from e2e.conftest import SignIn
from tests.forms_engine.test_form_application import make_case


def one_page() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.fixture
def contract() -> EmploymentContract:
    return make_case(variable_hours_unit=VariableHoursUnit.ONE_YEAR)


@pytest.mark.parametrize("width", [1366, 390])
def test_keep_a_calendar_and_make_the_list(
    page: Page, sign_in: SignIn, support_staff: User, contract: EmploymentContract, width: int
) -> None:
    admin_requests: list[str] = []
    page.on(
        "request",
        lambda request: (
            admin_requests.append(request.url)
            if urlparse(request.url).path.startswith("/admin/")
            else None
        ),
    )
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(support_staff)
    base = page.url.rstrip("/")
    site = contract.versions.get().site

    # 受入れ機関の書類の保管庫に年間カレンダーを足す
    page.goto(f"{base}/hosts/{contract.host_organization_id}/")
    page.locator("#host-document-create").click()
    page.locator("#id_kind").select_option("annual_calendar")
    page.locator("#id_site").select_option(str(site.pk))
    page.locator("#id_period_start").fill("2026-04-01")
    page.locator("#id_period_end").fill("2027-03-31")
    page.get_by_label("日本語", exact=True).check()
    page.get_by_label("中国語（簡体字）", exact=True).check()
    page.locator("#id_uploads").set_input_files(
        {"name": "カレンダー.pdf", "mimeType": "application/pdf", "buffer": one_page()}
    )
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.get_by_role("button", name="追加", exact=True).click()
    expect(page.get_by_text("書類を追加しました。")).to_be_visible()
    document = HostDocument.objects.get()
    expect(
        page.locator(f'#host-document-list [data-host-document="{document.pk}"]')
    ).to_contain_text("年間カレンダー")
    assert page.evaluate("document.documentElement.scrollWidth") <= width

    # 申請の書類の一覧: 年間カレンダーの行は保管庫から
    list_url = (
        f"{base}/workers/{contract.worker_id}/documents/application/?set_id=chg&basis_on=2026-10-18"
    )
    page.goto(list_url)
    calendar = page.locator('#application-set-rows [data-row="chg_t1-6-3"]')
    expect(calendar).to_have_attribute("data-status", "attached")
    expect(calendar.locator(f'[data-host-document="{document.pk}"]')).to_be_visible()

    # 提出用の PDF: 一覧表を作って先頭に
    page.locator("#submission-bundle-link").click()
    expect(page.locator("#id_make_list")).to_be_checked()
    expect(page.locator("#submission-photo")).to_be_visible()
    mark = page.locator("#id_mark-chg_t1-6-3")
    expect(mark).to_have_value("yes")
    page.locator("#id_mark-chg_t1-8-1").select_option("no")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.locator("#id_accept_warnings").check()
    page.locator("#submission-make").click()
    expect(page.get_by_text("提出用の PDF を作りました。")).to_be_visible()
    bundle = SubmissionBundle.objects.get()
    assert bundle.items[0]["source"] == "list"
    assert bundle.list_marks["chg_t1-6-3"] == "yes"
    expect(page.locator('#submission-items [data-source="list"]').first).to_be_visible()
    expect(page.locator('#submission-items [data-source="host_document"]')).to_have_count(1)
    assert admin_requests == []
