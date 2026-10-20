"""通しのテスト: 支援言語が中国語の外国人の雇用条件書（第1-6号）は、日中併記の旧い版＋
補足の頁が初期値（2026-10-20、ADR-0027 の追記）。

契約タブの版の生成の画面 → 「様式の言語」の初期値が日中併記で、併記の版を使わない知らせが
出ない → 生成 → 生成物の画面の言語が日中併記 → PDF は本体と別紙１ の 7 頁と補足の頁 1 頁。
管理画面には行かない。
"""

from io import BytesIO
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, expect
from pypdf import PdfReader

from apps.contracts.models import EmploymentContractVersion
from apps.core.models import SupportLanguage, User
from apps.forms_engine.models import GeneratedDocument
from e2e.conftest import SignIn
from e2e.test_contract_documents import version  # noqa: F401  fixture


@pytest.mark.parametrize("width", [1366, 390])
def test_generate_the_bilingual_employment_conditions(
    page: Page,
    sign_in: SignIn,
    support_staff: User,
    version: EmploymentContractVersion,  # noqa: F811
    width: int,
) -> None:
    worker = version.contract.worker
    worker.support_language = SupportLanguage.objects.get(code="zh-hans")
    worker.save()
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
    page.goto(
        f"{base}/workers/{worker.pk}/contracts/{version.contract_id}/versions/"
        f"{version.version}/generate/1-6/"
    )
    expect(page.locator("#id_language")).to_have_value("ja-zh")
    expect(page.locator("#bilingual-notice")).to_have_count(0)
    accept = page.locator("#id_accept_warnings")
    if accept.count():
        accept.check()
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.get_by_role("button", name="生成する").click()
    expect(page.locator("#document-status")).to_have_text("生成しました。")
    expect(page.locator("#document-language")).to_have_attribute("data-language", "ja-zh")
    document = GeneratedDocument.objects.get(worker=worker)
    assert document.template.language == "ja-zh" and document.page_count == 8
    with page.expect_download() as info:
        page.locator("#document-pdf").click()
    pdf = PdfReader(BytesIO(open(info.value.path(), "rb").read()))
    assert len(pdf.pages) == 8
    assert "雇用条件書の補足" in (pdf.pages[-1].extract_text() or "")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    assert admin_requests == []
