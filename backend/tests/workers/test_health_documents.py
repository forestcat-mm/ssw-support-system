"""要配慮添付（健康診断の書類。T23、ADR-0011）: 印・保持期限・ダウンロードの判定・監査ログ・
ファイル名を出さないこと・admin・保存先の設定・「基本」タブの画面。"""

from datetime import date
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import Client

from apps.core.checks import storage_problems
from apps.core.models import (
    Action,
    Attachment,
    AttachmentKind,
    AuditAction,
    AuditLog,
    RolePermission,
    User,
)
from services import attachments, worker_documents, workers
from tests.conftest import PDF_BYTES
from tests.core.factories import make_organization, make_staff, make_user, verified_client
from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 17)
FILENAME = "結核の所見あり_王芳.pdf"  # ファイル名に健康の情報が入っている例


def pdf(name: str = FILENAME) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, PDF_BYTES, content_type="application/pdf")


@pytest.fixture
def assigned() -> Any:
    """担当者（ログインユーザーを持つ支援担当者）。"""
    return make_user("tanto", roles=("support_staff",))


@pytest.fixture
def worker(assigned: Any) -> Any:
    return make_worker(assigned_staff=make_staff(name="佐藤", user=assigned))


def add(worker: Any, **kwargs: Any) -> Attachment:
    kwargs.setdefault("retention_until", None)
    created = worker_documents.add_documents(
        worker, kwargs.pop("kind", "health_check_record"), [pdf()], by=None, today=TODAY, **kwargs
    )
    return created[0]


def download(client: Client, attachment: Attachment) -> Any:
    return client.get(f"/attachments/{attachment.pk}/download/")


# --- 印・保持期限・対象 -------------------------------------------------------------------


def test_sensitive_flag_follows_the_kind(worker: Any) -> None:
    for kind in worker_documents.HEALTH_KINDS:
        assert add(worker, kind=kind).is_sensitive
    receipt = attachments.create_attachment(
        target=make_organization(),
        kind=AttachmentKind.FOOD_COUNCIL_CERTIFICATE,
        file=pdf("certificate.pdf"),
        created_by=None,
    )
    assert not receipt.is_sensitive
    # DB の制約で、種別と印の食い違いを拒否する（シグナルを通らない更新でも崩れない）
    sensitive = Attachment.objects.filter(is_sensitive=True).first()
    assert sensitive is not None
    with pytest.raises(IntegrityError), transaction.atomic():
        Attachment.objects.filter(pk=sensitive.pk).update(is_sensitive=False)


def test_sensitive_kinds_only_for_foreign_workers() -> None:
    with pytest.raises(ValueError):
        attachments.create_attachment(
            target=make_organization(),
            kind=AttachmentKind.HEALTH_CHECK_RECORD,
            file=pdf(),
            created_by=None,
        )


def test_add_documents_validates(worker: Any) -> None:
    with pytest.raises(ValidationError) as error:
        worker_documents.add_documents(
            worker, "support_record", [], retention_until=date(2026, 10, 16), by=None, today=TODAY
        )
    assert set(error.value.error_dict) == {"kind", "retention_until", "files"}
    with pytest.raises(ValidationError) as error:
        worker_documents.add_documents(
            worker,
            "health_check_record",
            [SimpleUploadedFile("memo.txt", b"x", content_type="text/plain")],
            retention_until=None,
            by=None,
            today=TODAY,
        )
    assert set(error.value.error_dict) == {"files"}
    attachment = add(worker, retention_until=date(2027, 3, 31))
    assert attachment.retention_until == date(2027, 3, 31)
    workers.deactivate(worker, by=None, reason="重複")
    with pytest.raises(ValidationError):
        add(worker)


def test_retention_past_the_guide_is_marked(worker: Any, assigned: Any) -> None:
    attachment = add(worker, retention_until=date(2026, 12, 31))
    rows = worker_documents.rows_for(assigned, worker, today=date(2027, 1, 1))
    assert [(row.attachment, row.expired) for row in rows] == [(attachment, True)]
    assert Attachment.objects.filter(pk=attachment.pk).exists()  # 自動では削除しない


# --- ダウンロードの判定 ----------------------------------------------------------------------


def test_who_can_download(worker: Any, assigned: Any) -> None:
    attachment = add(worker)
    admin = make_user("kanri", roles=("admin",))
    other = make_user("other", roles=("support_staff",))
    assert attachments.can_download(assigned, attachment)  # 担当者
    assert attachments.can_download(admin, attachment)  # workers.sensitive_documents
    assert not attachments.can_download(other, attachment)  # 担当でない支援担当者
    RolePermission.objects.filter(
        role__code="admin", action=Action.WORKERS_SENSITIVE_DOCUMENTS
    ).delete()
    admin = User.objects.get(pk=admin.pk)  # 操作はユーザーのオブジェクトごとにキャッシュされる
    assert not attachments.can_download(admin, attachment)  # 操作を外した管理者
    # 担当者を変えると、前の担当者はダウンロードできない
    worker.assigned_staff = make_staff(name="鈴木")
    worker.save()
    assert not attachments.can_download(assigned, attachment)


def test_download_view_allows_and_denies_with_audit(worker: Any, assigned: Any) -> None:
    attachment = add(worker)
    response = download(verified_client(assigned), attachment)
    assert response.status_code == 200
    assert b"".join(response.streaming_content) == PDF_BYTES
    assert AuditLog.objects.filter(
        action=AuditAction.DOWNLOAD, object_id=str(attachment.pk)
    ).exists()

    other = make_user("other", roles=("support_staff",))
    assert download(verified_client(other), attachment).status_code == 403
    denied = AuditLog.objects.get(action=AuditAction.DOWNLOAD_DENIED)
    assert (denied.actor, denied.object_id) == (other, str(attachment.pk))
    assert FILENAME not in denied.object_repr and denied.context == {"kind": "health_check_record"}


def test_non_sensitive_denial_is_not_logged_as_denied() -> None:
    attachment = attachments.create_attachment(
        target=make_organization(),
        kind=AttachmentKind.FOOD_COUNCIL_CERTIFICATE,
        file=pdf("certificate.pdf"),
        created_by=None,
    )
    nobody = make_user("norole")
    assert download(verified_client(nobody), attachment).status_code == 403
    assert not AuditLog.objects.filter(action=AuditAction.DOWNLOAD_DENIED).exists()


# --- ファイル名を記録・表示に出さない ---------------------------------------------------------


def test_audit_log_does_not_keep_the_file_name(worker: Any) -> None:
    attachment = add(worker)
    attachments.void_attachment(attachment, by=None, reason="別の人の書類")
    entries = AuditLog.objects.filter(
        content_type__model="attachment", object_id=str(attachment.pk)
    )
    assert {entry.action for entry in entries} == {AuditAction.CREATE, AuditAction.UPDATE}
    for entry in entries:
        assert FILENAME not in str(entry.changes) and FILENAME not in entry.object_repr
    created = entries.get(action=AuditAction.CREATE)
    assert created.changes["original_filename"] == [None, "***"]
    assert (
        created.object_repr == f"健康診断個人票（参考様式第1-3号。日本語訳を含む） #{attachment.pk}"
    )


def test_files_have_no_url(worker: Any) -> None:
    """公開 URL も署名付き URL も作れない（ファイルは認可つきのダウンロードからだけ）。"""
    attachment = add(worker)
    with pytest.raises(PermissionError):
        _ = attachment.file.url


def test_media_is_not_served(worker: Any, admin_client: Client) -> None:
    attachment = add(worker)
    assert admin_client.get(f"/media/{attachment.file.name}").status_code == 404
    assert admin_client.get(f"/{attachment.file.name}").status_code == 404


def test_storage_settings_must_stay_private() -> None:
    s3 = "storages.backends.s3.S3Storage"
    assert storage_problems(s3, {"bucket_name": "b", "default_acl": "private"}) == []
    assert storage_problems(s3, {"bucket_name": "b"}) == []
    assert len(storage_problems(s3, {"default_acl": "public-read"})) == 1
    assert len(storage_problems(s3, {"custom_domain": "files.example.com"})) == 1
    assert len(storage_problems(s3, {"querystring_auth": False})) == 1
    assert storage_problems("django.core.files.storage.FileSystemStorage", {}) == []


def test_admin_hides_the_file_name_and_link(worker: Any, admin_client: Client) -> None:
    sensitive = add(worker)
    plain = attachments.create_attachment(
        target=make_organization(),
        kind=AttachmentKind.FOOD_COUNCIL_CERTIFICATE,
        file=pdf("certificate.pdf"),
        created_by=None,
    )
    listing = admin_client.get("/admin/core/attachment/").content.decode()
    assert FILENAME not in listing and "certificate.pdf" in listing
    detail = admin_client.get(f"/admin/core/attachment/{sensitive.pk}/change/").content.decode()
    assert FILENAME not in detail
    assert f"/attachments/{sensitive.pk}/download/" not in detail
    plain_detail = admin_client.get(f"/admin/core/attachment/{plain.pk}/change/").content.decode()
    assert f"/attachments/{plain.pk}/download/" in plain_detail


# --- 「基本」タブの画面 -----------------------------------------------------------------------


def detail_url(worker: Any) -> str:
    return f"/workers/{worker.pk}/"


def test_basic_tab_shows_file_names_only_to_who_can_download(worker: Any, assigned: Any) -> None:
    attachment = add(worker)
    html = verified_client(assigned).get(detail_url(worker)).content.decode()
    assert FILENAME in html and f"/attachments/{attachment.pk}/download/" in html
    other = make_user("other", roles=("support_staff",))
    html = verified_client(other).get(detail_url(worker)).content.decode()
    assert 'id="health-documents"' in html and "健康診断個人票" in html
    assert FILENAME not in html and f"/attachments/{attachment.pk}/download/" not in html
    assert "（ファイル名は表示しません）" in html
    entry = AuditLog.objects.filter(action=AuditAction.VIEW, object_id=str(worker.pk)).latest("pk")
    assert entry.context["shown"]["health_documents"] == [attachment.pk]


def test_upload_and_void_on_screen_do_not_show_the_name(worker: Any) -> None:
    """担当でない支援担当者でも登録・取消はできる（workers.manage）。ファイル名は通知文にも出ない。"""
    other = make_user("other", roles=("support_staff",))
    client = verified_client(other)
    url = f"/workers/{worker.pk}/health-documents/new/"
    page = client.get(url)
    response = client.post(
        url,
        {
            "once_token": page.context["once_token"],
            "kind": "health_declaration",
            "uploads": [pdf()],
            "retention_until": "2027-03-31",
        },
        follow=True,
    )
    html = response.content.decode()
    assert "健康診断の書類を添付しました。" in html and FILENAME not in html
    attachment = Attachment.objects.get(kind="health_declaration")
    void_url = f"/workers/{worker.pk}/health-documents/{attachment.pk}/void/"
    confirm = client.get(void_url)
    assert FILENAME not in confirm.content.decode()
    response = client.post(
        void_url,
        {"once_token": confirm.context["once_token"], "reason": "別の人の書類"},
        follow=True,
    )
    assert FILENAME not in response.content.decode()
    attachment.refresh_from_db()
    assert attachment.voided_at is not None
    assert client.get(void_url).status_code == 404


def test_viewer_and_deactivated_worker_cannot_upload(worker: Any) -> None:
    viewer = make_user("viewer", roles=("support_staff",))
    viewer.user_roles.get().role.permissions.filter(action="workers.manage").delete()
    url = f"/workers/{worker.pk}/health-documents/new/"
    assert verified_client(viewer).get(url).status_code == 403
    admin = make_user("kanri", roles=("admin",))
    workers.deactivate(worker, by=None, reason="重複")
    assert verified_client(admin).get(url).status_code == 403
