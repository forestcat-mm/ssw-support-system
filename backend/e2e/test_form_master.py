"""通しのテスト: 様式マスタ（F13。T53）。

設定の「様式マスタ」で版の一覧と詳細（出典・SHA-256）を確かめる。版が 1 つだけの様式は停止できない
ことを画面で確かめる。管理画面には行かない。
"""

from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Request, expect

from apps.core.models import User
from apps.forms_engine.models import FormTemplate
from e2e.conftest import SignIn
from services import form_templates


@pytest.fixture
def loaded() -> None:
    form_templates.load_all()


@pytest.mark.parametrize("width", [1366, 390])
def test_versions_and_suspension(
    page: Page, sign_in: SignIn, administrator: User, loaded: None, width: int
) -> None:
    admin_requests: list[str] = []

    def watch(request: Request) -> None:
        if urlparse(request.url).path.startswith("/admin/"):
            admin_requests.append(request.url)

    page.on("request", watch)
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(administrator)
    base = page.url.rstrip("/")
    page.goto(f"{base}/settings/")
    page.get_by_role("link", name="様式マスタ").click()
    template = FormTemplate.objects.get(form_id="3-6-2")
    row = page.locator(f'#form-master [data-version="{template.pk}"]')
    expect(row).to_contain_text("使用中")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    row.get_by_role("link").click()
    expect(page.locator("#form-version")).to_contain_text(template.original_sha256)
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.get_by_label("理由").fill("欄の位置の誤り")
    page.get_by_role("button", name="この版を停止する").click()
    expect(page.locator("#form-suspend")).to_contain_text("使っている版はこの 1 つだけです")
    assert admin_requests == []
