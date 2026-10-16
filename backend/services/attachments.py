"""添付ファイルの検査・作成。種別ごとに許可する拡張子と最大サイズを持つ。"""

from __future__ import annotations

from datetime import date
from typing import Any

from django.core.files import File
from django.db import models

from apps.core.models import Attachment, AttachmentKind, User
from services.files import MB, FilePolicy, sha256_of, validate_file

POLICIES: dict[AttachmentKind, FilePolicy] = {
    AttachmentKind.FOOD_COUNCIL_CERTIFICATE: FilePolicy(
        extensions=frozenset({"pdf", "png", "jpg", "jpeg"}), max_bytes=10 * MB
    ),
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
