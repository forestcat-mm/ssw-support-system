"""通しのテスト: 署名済み書類の回収（T33、ADR-0022）。

回収のタスク（生成から設定の日数）→ タスクの詳細から帳票タブ → 署名済みのスキャンを添付 →
帳票タブで「署名済み」→ タスクが自動で完了。admin は開かない。
"""

import datetime
from pathlib import Path
from urllib.parse import urlparse

import pytest
from django.utils import timezone
from playwright.sync_api import Page, Request, expect

from apps.core.models import User
from apps.filings.models import Filing, FilingStatus
from apps.forms_engine.models import FormTemplate, GeneratedDocument, GenerationStatus
from e2e.conftest import SignIn
from services import form_signatures, form_templates
from tests.conftest import PDF_BYTES
from tests.contracts.factories import make_contract, site_with_categories
from tests.core.factories import make_organization, make_staff
from tests.workers.factories import make_worker


@pytest.fixture
def task() -> Filing:
    form_templates.load_all()
    make_organization()
    worker = make_worker(assigned_staff=make_staff(name="佐藤 花子", position="支援担当"))
    make_contract(worker, site=site_with_categories())
    generated = timezone.localdate() - datetime.timedelta(days=20)
    GeneratedDocument.objects.create(
        template=FormTemplate.objects.get(form_id="1-17", language="ja"),
        target=worker,
        worker=worker,
        basis_on=generated,
        snapshot="{}",
        status=GenerationStatus.COMPLETED,
        generated_at=timezone.make_aware(datetime.datetime.combine(generated, datetime.time(10))),
    )
    [filing] = form_signatures.raise_collection_tasks(timezone.localdate())
    return filing


@pytest.mark.parametrize("width", [1366, 390])
def test_attach_the_signed_copy_from_the_collection_task(
    page: Page, sign_in: SignIn, support_staff: User, task: Filing, width: int, tmp_path: Path
) -> None:
    admin_requests: list[str] = []

    def watch(request: Request) -> None:
        if urlparse(request.url).path.startswith("/admin/"):
            admin_requests.append(request.url)

    page.on("request", watch)
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(support_staff)
    base = page.url.rstrip("/")
    page.goto(f"{base}/filings/{task.pk}/")
    expect(page.get_by_text("帳票タブで署名済みの書類を添付すると")).to_be_visible()
    page.locator("#open-documents").click()

    row = page.locator("#generated-documents tbody tr").first
    expect(row).to_contain_text("署名待ち")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    row.get_by_role("link", name="署名済みの書類を添付").click()
    scan = tmp_path / "signed.pdf"
    scan.write_bytes(PDF_BYTES)
    page.get_by_label("署名済みのスキャン").set_input_files(str(scan))
    page.get_by_label("署名日").fill(timezone.localdate().isoformat())
    page.get_by_role("button", name="保存する").click()

    expect(page.get_by_text("署名済みの書類を添付しました。")).to_be_visible()
    expect(page.locator("#generated-documents tbody tr").first).to_contain_text("署名済み")
    task.refresh_from_db()
    assert task.status == FilingStatus.COMPLETED
    assert admin_requests == []
