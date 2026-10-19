"""外国人の健康診断の書類（要配慮の添付。第6章「要配慮個人情報を含む添付」、T23、ADR-0011）。

- 種別: 健康診断個人票（日本語訳を含む）／受診者の申告書／医師の意見書。外国人にだけ添付する。
- ダウンロードは services.attachments.can_download（担当者と、workers.sensitive_documents を
  持つ人だけ）。
- 保持期限の目安は任意。過ぎても自動では削除しない。
- 誤りは添付の取消（理由必須。ファイルは消さない）。
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError
from django.core.files import File
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.models import SENSITIVE_KINDS, Attachment, AttachmentKind, User
from apps.workers.models import ForeignWorker
from services import attachments, workers
from services.authorization import AnyUser

HEALTH_KINDS: tuple[AttachmentKind, ...] = (
    AttachmentKind.HEALTH_CHECK_RECORD,
    AttachmentKind.HEALTH_DECLARATION,
    AttachmentKind.DOCTOR_OPINION,
)
assert set(HEALTH_KINDS) == SENSITIVE_KINDS


def add_documents(
    worker: ForeignWorker,
    kind: str,
    files: Iterable[File[Any]],
    *,
    retention_until: datetime.date | None,
    by: User | None,
    examined_on: datetime.date | None = None,
    today: datetime.date | None = None,
) -> list[Attachment]:
    """健康診断の書類を添付する（1 回に同じ種別の複数のファイル。受診日は必須。修正 1 回目 B8）。"""
    today = today or timezone.localdate()
    errors: dict[str, list[ValidationError]] = {}

    def add(key: str, message: str, code: str) -> None:
        errors.setdefault(key, []).append(ValidationError(message, code=code))

    if not workers.is_active(worker):
        add("__all__", _("This foreign worker is deactivated."), "deactivated")
    if kind not in HEALTH_KINDS:
        add("kind", _("Select a valid choice."), "invalid_choice")
    if retention_until is not None and retention_until < today:
        add("retention_until", _("The date cannot be in the past."), "past")
    if examined_on is None:
        add("examined_on", _("Enter the date of the health check."), "required")
    elif examined_on > today:
        add("examined_on", _("The date cannot be in the future."), "future")
    files = list(files)
    if not files:
        add("files", _("Select a file."), "required")
    elif kind in HEALTH_KINDS:
        for file in files:
            try:
                attachments.validate_upload(kind, file)
            except ValidationError as error:
                errors.setdefault("files", []).extend(
                    ValidationError(message) for message in error.messages
                )
    if errors:
        raise ValidationError(errors)
    with transaction.atomic():
        created = []
        for file in files:
            attachment = attachments.create_attachment(
                target=worker, kind=AttachmentKind(kind), file=file, created_by=by
            )
            attachment.retention_until = retention_until
            attachment.examined_on = examined_on
            attachment.save(update_fields=["retention_until", "examined_on", "updated_at"])
            created.append(attachment)
        return created


def documents_of(worker: ForeignWorker) -> models.QuerySet[Attachment]:
    """外国人の健康診断の書類（取り消したものも含む。新しい順）。"""
    return (
        Attachment.objects.filter(
            content_type__app_label="workers",
            content_type__model="foreignworker",
            object_id=worker.pk,
            kind__in=HEALTH_KINDS,
        )
        .select_related("created_by", "voided_by", "content_type")
        .order_by("-created_at", "-pk")
    )


@dataclass(frozen=True)
class DocumentRow:
    attachment: Attachment
    can_download: bool  # ファイル名とリンクを出すか（出せない人には種別と日付だけ）
    expired: bool  # 保持期限の目安を過ぎた


def rows_for(
    user: AnyUser, worker: ForeignWorker, *, today: datetime.date | None = None
) -> list[DocumentRow]:
    today = today or timezone.localdate()
    return [
        DocumentRow(
            attachment=attachment,
            can_download=attachments.can_download(user, attachment),
            expired=attachment.retention_until is not None and attachment.retention_until < today,
        )
        for attachment in documents_of(worker)
    ]
