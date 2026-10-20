"""通しのテスト: 一括申請（T48）。

一括申請の画面で、外国人ごとの検査が通っていることを確かめ、申請ごとの入力を開いて保存し、
Excel を作ってダウンロードする。管理画面には行かない。
"""

from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Request, expect

from apps.core.models import User
from apps.forms_engine.models import BulkApplication
from e2e.conftest import SignIn
from services import form_templates
from tests.forms_engine.bulk_cases import complete_application


@pytest.fixture
def application() -> BulkApplication:
    form_templates.load_all()
    return complete_application("extension")


def watch_admin(page: Page) -> list[str]:
    seen: list[str] = []

    def watch(request: Request) -> None:
        if urlparse(request.url).path.startswith("/admin/"):
            seen.append(request.url)

    page.on("request", watch)
    return seen


@pytest.mark.parametrize("width", [1366, 390])
def test_check_inputs_and_generate(
    page: Page,
    sign_in: SignIn,
    administrator: User,
    application: BulkApplication,
    width: int,
) -> None:
    admin_requests = watch_admin(page)
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(administrator)
    base = page.url.rstrip("/")
    page.goto(f"{base}/forms/bulk/")
    page.locator(f'[data-bulk="{application.pk}"] a').click()
    expect(page.get_by_role("heading", level=1)).to_contain_text("在留期間更新許可申請")
    rows = page.locator("#bulk-rows tbody tr")
    expect(rows).to_have_count(2)
    expect(page.locator('[data-problems="0"]')).to_have_count(2)
    assert page.evaluate("document.documentElement.scrollWidth") <= width

    rows.first.get_by_role("link", name="入力").click()
    expect(page.get_by_role("heading", level=1)).to_contain_text("申請ごとの入力")
    expect(page.get_by_label("職業")).to_have_value("会社員")
    expect(page.locator("#bulk-sensitive")).to_contain_text("犯罪を理由とする処分")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.get_by_role("button", name="保存する").click()
    expect(page.get_by_text("保存しました")).to_be_visible()

    page.get_by_role("button", name="Excel を作る").click()
    expect(page.get_by_text("1 個のファイルを作りました。")).to_be_visible()
    with page.expect_download() as download:
        page.locator("#files a.attachment-list__name").first.click()
    assert download.value.suggested_filename.endswith(".xlsm")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    assert admin_requests == []
