"""通しのテスト: 健康診断の書類（要配慮の添付）は、担当者はダウンロードでき、担当でない人には
ファイル名もリンクも出ず、直接開いても 403 になる（T23、ADR-0011）。"""

from datetime import date

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from playwright.sync_api import Page, expect

from apps.core.models import Attachment, User
from apps.workers.models import ForeignWorker
from e2e.conftest import SignIn
from services import worker_documents
from tests.conftest import PDF_BYTES
from tests.core.factories import make_staff, make_user
from tests.workers.factories import make_worker

FILENAME = "健康診断個人票_王芳.pdf"


@pytest.fixture
def worker(support_staff: User) -> ForeignWorker:
    """担当者は support_staff（ログインする支援担当者）。"""
    return make_worker(assigned_staff=make_staff(name="佐藤 花子", user=support_staff))


@pytest.fixture
def document(worker: ForeignWorker) -> Attachment:
    file = SimpleUploadedFile(FILENAME, PDF_BYTES, content_type="application/pdf")
    return worker_documents.add_documents(
        worker,
        "health_check_record",
        [file],
        retention_until=date(2027, 3, 31),
        examined_on=date(2026, 10, 1),
        by=None,
    )[0]


@pytest.mark.parametrize("width", [1366, 390])
def test_assigned_staff_can_download(
    page: Page,
    sign_in: SignIn,
    support_staff: User,
    worker: ForeignWorker,
    document: Attachment,
    width: int,
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(support_staff)
    base = page.url.rstrip("/")
    page.goto(f"{base}/workers/{worker.pk}/")
    section = page.locator("#health-documents")
    link = section.get_by_role("link", name=FILENAME)
    expect(link).to_be_visible()
    with page.expect_download() as info:
        link.click()
    assert info.value.suggested_filename == FILENAME
    assert page.evaluate("document.documentElement.scrollWidth") <= width


@pytest.mark.parametrize("width", [1366, 390])
def test_other_staff_sees_no_name_and_gets_403(
    page: Page, sign_in: SignIn, worker: ForeignWorker, document: Attachment, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    other = make_user("other", roles=("support_staff",))
    sign_in(other)
    base = page.url.rstrip("/")
    page.goto(f"{base}/workers/{worker.pk}/")
    section = page.locator("#health-documents")
    expect(section).to_contain_text("健康診断個人票")
    expect(section).to_contain_text("（ファイル名は表示しません）")
    expect(section).not_to_contain_text(FILENAME)
    expect(section.locator(f'a[href="/attachments/{document.pk}/download/"]')).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    response = page.request.get(f"{base}/attachments/{document.pk}/download/")
    assert response.status == 403
