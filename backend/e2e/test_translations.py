"""通しのテスト: 支援計画書（第1-17号）の中国語の訳文（自作。T43）。

支援言語が中国語の外国人の支援計画書の生成の画面 → 併記の版を使わない知らせと
「様式の言語」→ 中国語の訳文を選んで確かめ直す → 生成 → 生成物の画面の言語が訳文 →
帳票タブに日本語の版とは別の行で出る。管理画面には行かない。
"""

from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, expect

from apps.core.models import SupportLanguage, User
from apps.forms_engine.models import GeneratedDocument
from apps.workers.models import ForeignWorker
from e2e.conftest import SignIn
from e2e.test_form_1_17 import worker  # noqa: F401  fixture


@pytest.mark.parametrize("width", [1366, 390])
def test_generate_the_chinese_translation_of_the_support_plan(
    page: Page,
    sign_in: SignIn,
    support_staff: User,
    worker: ForeignWorker,  # noqa: F811
    width: int,
) -> None:
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
    plan = worker.support_plans.get()
    version = plan.versions.get()
    page.goto(
        f"{base}/workers/{worker.pk}/support/plans/{plan.pk}/versions/{version.version}/generate/"
    )
    expect(page.locator("#bilingual-notice")).to_be_visible()
    expect(page.locator("#id_language")).to_have_value("ja")
    page.locator("#id_language").select_option("zh-hans")
    page.locator("#id_propriety").check()
    page.locator('button[name="action"][value="check"]').click()
    expect(page.locator("#id_language")).to_have_value("zh-hans")
    accept = page.locator("#id_accept_warnings")
    if accept.count():
        accept.check()
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.locator('button[name="action"][value="generate"]').click()
    expect(page.locator("#document-language")).to_have_attribute("data-language", "zh-hans")
    expect(page.locator("#document-language")).to_contain_text("中国語（簡体字）の訳文")
    document = GeneratedDocument.objects.get(worker=worker)
    assert document.template.language == "zh-hans"
    page.goto(f"{base}/workers/{worker.pk}/documents/")
    expect(page.locator(f'[data-document="{document.pk}"]')).to_contain_text("訳文")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    assert admin_requests == []
