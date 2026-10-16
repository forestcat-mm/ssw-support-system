import hashlib
from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from apps.core.models import Attachment, AttachmentKind, Organization, User
from services.attachments import create_attachment
from tests.conftest import PDF_BYTES, PNG_BYTES

from .factories import make_organization

pytestmark = pytest.mark.django_db

INLINE = "core-attachment-content_type-object_id"


def test_create_attachment_fills_metadata(admin_user: User) -> None:
    organization = make_organization()
    attachment = create_attachment(
        target=organization,
        kind=AttachmentKind.FOOD_COUNCIL_CERTIFICATE,
        file=ContentFile(PDF_BYTES, name="構成員証明書_山田.pdf"),
        created_by=admin_user,
        valid_until=date(2027, 3, 31),
    )
    attachment.refresh_from_db()
    assert attachment.sha256 == hashlib.sha256(PDF_BYTES).hexdigest()
    assert attachment.size == len(PDF_BYTES)
    assert attachment.original_filename == "構成員証明書_山田.pdf"
    assert attachment.created_by == admin_user
    # 保存パスに元のファイル名（個人名を含みうる）を使わない
    assert attachment.file.name.startswith("attachments/")
    assert "山田" not in attachment.file.name
    assert attachment.file.read() == PDF_BYTES
    assert list(organization.attachments.all()) == [attachment]


@pytest.mark.parametrize(
    ("name", "content", "code"),
    [
        ("certificate.exe", PDF_BYTES, "invalid_extension"),
        ("certificate.pdf", PNG_BYTES, "signature_mismatch"),
        ("certificate.pdf", PDF_BYTES + b"0" * (10 * 1024 * 1024), "file_too_large"),
    ],
)
def test_create_attachment_rejects_disallowed_files(name: str, content: bytes, code: str) -> None:
    organization = make_organization()
    with pytest.raises(ValidationError) as excinfo:
        create_attachment(
            target=organization,
            kind=AttachmentKind.FOOD_COUNCIL_CERTIFICATE,
            file=ContentFile(content, name=name),
            created_by=None,
        )
    assert excinfo.value.code == code
    assert Attachment.objects.count() == 0


def _organization_form(file: SimpleUploadedFile) -> dict[str, object]:
    return {
        "name": "テスト登録支援機関",
        "registration_number": "19登-000000",
        "registration_expires_on": "2029-03-31",
        "postal_code": "100-0001",
        "address": "東京都千代田区千代田1-1",
        "food_council_joined_on": "2024-04-01",
        f"{INLINE}-TOTAL_FORMS": "1",
        f"{INLINE}-INITIAL_FORMS": "0",
        f"{INLINE}-MIN_NUM_FORMS": "0",
        f"{INLINE}-MAX_NUM_FORMS": "1000",
        f"{INLINE}-0-kind": AttachmentKind.FOOD_COUNCIL_CERTIFICATE,
        f"{INLINE}-0-file": file,
        f"{INLINE}-0-valid_until": "2027-03-31",
    }


def test_admin_inline_upload(admin_client: Client, admin_user: User) -> None:
    file = SimpleUploadedFile("cert.pdf", PDF_BYTES, content_type="application/pdf")
    response = admin_client.post("/admin/core/organization/add/", _organization_form(file))
    assert response.status_code == 302

    organization = Organization.objects.get()
    attachment = organization.attachments.get()
    assert attachment.sha256 == hashlib.sha256(PDF_BYTES).hexdigest()
    assert attachment.original_filename == "cert.pdf"
    assert attachment.created_by == admin_user
    assert organization.created_by == admin_user


def test_admin_inline_rejects_disallowed_file(admin_client: Client) -> None:
    file = SimpleUploadedFile("cert.pdf", PNG_BYTES, content_type="application/pdf")
    response = admin_client.post("/admin/core/organization/add/", _organization_form(file))
    assert response.status_code == 200
    assert "一致しません" in response.content.decode()
    assert Organization.objects.count() == 0
    assert Attachment.objects.count() == 0


def test_admin_cannot_add_or_delete_attachment_directly(admin_client: Client) -> None:
    attachment = create_attachment(
        target=make_organization(),
        kind=AttachmentKind.FOOD_COUNCIL_CERTIFICATE,
        file=ContentFile(PDF_BYTES, name="cert.pdf"),
        created_by=None,
    )
    assert admin_client.get("/admin/core/attachment/add/").status_code == 403
    assert admin_client.get(f"/admin/core/attachment/{attachment.pk}/delete/").status_code == 403
