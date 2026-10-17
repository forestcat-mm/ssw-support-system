"""添付ファイルの検査・作成。種別ごとに許可する拡張子と最大サイズを持つ。"""

from __future__ import annotations

from datetime import date
from typing import Any

from django.core.exceptions import ValidationError
from django.core.files import File
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.models import Attachment, AttachmentKind, User
from services.files import MB, FilePolicy, sha256_of, validate_file

DOCUMENT_POLICY = FilePolicy(extensions=frozenset({"pdf", "png", "jpg", "jpeg"}), max_bytes=10 * MB)

POLICIES: dict[AttachmentKind, FilePolicy] = {
    AttachmentKind.FOOD_COUNCIL_CERTIFICATE: DOCUMENT_POLICY,
    AttachmentKind.MEMBERSHIP_CERTIFICATE: DOCUMENT_POLICY,
    AttachmentKind.COOPERATION_CONFIRMATION: DOCUMENT_POLICY,
    AttachmentKind.SUPPORT_CONTRACT: DOCUMENT_POLICY,
    AttachmentKind.EQUAL_PAY_EVIDENCE: DOCUMENT_POLICY,
    AttachmentKind.VARIABLE_HOURS_DOCUMENTS: DOCUMENT_POLICY,
    AttachmentKind.SUPPORT_RECORD: DOCUMENT_POLICY,
    AttachmentKind.PRE_GUIDANCE_CONFIRMATION: DOCUMENT_POLICY,
    AttachmentKind.ORIENTATION_CONFIRMATION: DOCUMENT_POLICY,
}


def policy_for(kind: AttachmentKind | str) -> FilePolicy:
    return POLICIES[AttachmentKind(kind)]


def validate_upload(kind: AttachmentKind | str, file: File[Any]) -> None:
    validate_file(file, policy_for(kind))


def fill_file_metadata(attachment: Attachment, file: File[Any]) -> None:
    """保存前に元ファイル名・サイズ・SHA-256 を埋める。"""
    attachment.original_filename = file.name or ""
    attachment.size = file.size or 0
    attachment.sha256 = sha256_of(file)


def create_attachment(
    *,
    target: models.Model,
    kind: AttachmentKind,
    file: File[Any],
    created_by: User | None,
    valid_until: date | None = None,
) -> Attachment:
    validate_upload(kind, file)
    attachment = Attachment(
        target=target,
        kind=kind,
        valid_until=valid_until,
        created_by=created_by,
        updated_by=created_by,
    )
    fill_file_metadata(attachment, file)
    attachment.file.save(file.name or "", file, save=False)
    attachment.save()
    return attachment


def active(queryset: models.QuerySet[Attachment]) -> models.QuerySet[Attachment]:
    """取り消していない添付（有効期限・最新の証明書・証拠の有無の判定はこれを使う）。"""
    return queryset.filter(voided_at__isnull=True)


def void_attachment(attachment: Attachment, *, by: User | None, reason: str) -> None:
    """添付を取り消す（誤った添付の訂正）。ファイルは削除せず、打ち消して残す。

    監査ログには添付の更新（取消の日時・者・理由の前後）として残る。
    """
    if attachment.voided_at is not None:
        raise ValidationError(_("This attachment is already voided."), code="voided")
    if not reason.strip():
        raise ValidationError(_("Enter the reason for voiding."), code="void_reason")
    attachment.voided_at = timezone.now()
    attachment.voided_by = by
    attachment.void_reason = reason.strip()
    attachment.updated_by = by
    attachment.save(
        update_fields=["voided_at", "voided_by", "void_reason", "updated_by", "updated_at"]
    )
