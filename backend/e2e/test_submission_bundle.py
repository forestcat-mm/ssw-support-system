"""通しのテスト: 不足の書類の一括生成と提出用の統合 PDF（T42）。

申請の書類の一覧で行ごとの検査の結果（作れない・作れる）を見る →「不足の書類をまとめて生成」→
第3-1号だけを選んで生成 → 一覧に戻る →「提出用の PDF を作る」→ 提出書類一覧表の行に PDF を足し、
警告を確かめて作る → 統合 PDF の画面（頁数・ダウンロード）→ 一覧の履歴に出る。管理画面には行かない。
"""

import io
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, expect
from pypdf import PdfWriter

from apps.contracts.models import EmploymentContract
from apps.core.models import User
from apps.forms_engine.models import SubmissionBundle
from e2e.conftest import SignIn
from e2e.test_application_documents import contract  # noqa: F401  fixture


def one_page() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.mark.parametrize("width", [1366, 390])
def test_bulk_generation_and_submission_pdf(
    page: Page,
    sign_in: SignIn,
    support_staff: User,
    contract: EmploymentContract,  # noqa: F811
    width: int,
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
    list_url = (
        f"{base}/workers/{contract.worker_id}/documents/application/?set_id=chg&basis_on=2026-10-18"
    )
    page.goto(list_url)
    rows = page.locator("#application-set-rows")
    plan = rows.locator('[data-row="chg_t1-18-1"]')  # 第1-17号（支援計画が無い）
    pledge = rows.locator('[data-row="chg_ind-2-1"]')  # 第3-1号
    expect(plan.locator('[data-check="blocked"]')).to_have_text("作れない")
    expect(pledge.locator('[data-check="ok"]')).to_have_text("作れる")
    assert page.evaluate("document.documentElement.scrollWidth") <= width

    # 一括生成: 生成不可の行は選べない。第3-1号だけを選んで生成する
    page.locator("#application-bulk-link").click()
    items = page.locator("#bulk-candidates")
    expect(items.locator('[data-row="chg_t1-18-1"]')).to_have_attribute("data-check", "blocked")
    expect(items.locator('[data-row="chg_t1-18-1"] input[name="rows"]')).to_have_count(0)
    expect(items.locator('[data-row="chg_t1-18-1"]')).to_contain_text("支援計画がありません。")
    for box in items.locator('input[name="rows"]').all():
        if box.get_attribute("value") != "chg_ind-2-1":
            box.uncheck()
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    accept = page.locator("#id_accept_warnings")
    if accept.count():
        accept.check()
    page.locator("#bulk-generate").click()
    expect(page.get_by_text("1 件の生成を依頼しました。")).to_be_visible()
    expect(pledge).to_have_attribute("data-status", "generated")

    # 提出用の PDF: 一覧表の行に PDF を足して作る
    page.locator("#submission-bundle-link").click()
    first = page.locator(".submission-item").first
    expect(first).to_have_attribute("data-row", "chg_t1-2-1")  # 提出書類一覧表が先頭
    expect(page.locator('[data-row="chg_ind-2-1"] [data-source="generated"]')).to_be_visible()
    first.locator('input[type="file"]').set_input_files(
        {"name": "一覧表.pdf", "mimeType": "application/pdf", "buffer": one_page()}
    )
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    # まず保存だけ（下書きに残る）→ 作り直すときに使い回せる（T42-D3 の直し）
    page.locator("#submission-save").click()
    expect(page.get_by_text("提出用の書類を保存しました。")).to_be_visible()
    first = page.locator(".submission-item").first
    expect(first.locator("[data-saved-file]")).to_have_count(1)
    page.locator("#id_accept_warnings").check()
    page.locator("#submission-make").click()
    expect(page.get_by_text("提出用の PDF を作りました。")).to_be_visible()
    bundle = SubmissionBundle.objects.get()
    expect(page.locator("#submission-pages")).to_have_text(str(bundle.page_count))
    assert bundle.items[0]["row"] == "chg_t1-2-1"
    with page.expect_download() as download:
        page.locator("#submission-download").click()
    assert download.value.suggested_filename.startswith("submission_chg_20261018_")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.goto(list_url)
    expect(page.locator(f'#submission-bundles [data-bundle="{bundle.pk}"]')).to_be_visible()
    assert admin_requests == []
