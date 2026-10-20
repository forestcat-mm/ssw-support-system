"""提出用の統合 PDF の下書き（システムの外の書類の保存。T42-D3 の直し）。

統合 PDF の画面でアップロードしたシステムの外の書類（提出書類一覧表の行ごと）を、外国人×申請の種類の
下書きに保存し、作り直すときに使い回す。取り除く・差し替えるは取消（理由つき。行とファイルは残る）。
作成・取消は監査ログに残る（添付と紐づけの行は AUDITED_MODELS）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from django.core.exceptions import ValidationError
from django.core.files import File
from django.db import transaction
from django.utils.translation import gettext as _

from apps.core.models import Attachment, AttachmentKind, User
from apps.forms_engine.models import SubmissionDraft, SubmissionDraftFile
from apps.workers.models import ForeignWorker
from services import attachments, pdf_pages


def draft_for(worker: ForeignWorker, set_id: str) -> SubmissionDraft | None:
    return SubmissionDraft.objects.filter(worker=worker, set_id=set_id).first()


def files_by_row(draft: SubmissionDraft | None) -> dict[str, list[Attachment]]:
    """行の ID → 取り消していない添付（古い順）。"""
    if draft is None:
        return {}
    result: dict[str, list[Attachment]] = {}
    for item in (
        draft.files.filter(attachment__voided_at__isnull=True)
        .select_related("attachment")
        .order_by("pk")
    ):
        result.setdefault(item.row_id, []).append(item.attachment)
    return result


def readable_errors(uploads: Mapping[str, Sequence[File[Any]]]) -> dict[str, str]:
    """PDF・画像として読めないアップロード（行の ID → 理由の code）。"""
    errors: dict[str, str] = {}
    for row_id, files in uploads.items():
        for file in files:
            file.seek(0)
            try:
                pdf_pages.as_pdf(file.read())
            except pdf_pages.UnreadableFile as unreadable:
                errors[row_id] = unreadable.code
            finally:
                file.seek(0)
    return errors


@transaction.atomic
def save(
    worker: ForeignWorker,
    set_id: str,
    uploads: Mapping[str, Sequence[File[Any]]],
    *,
    remove: Iterable[int] = (),
    by: User | None,
) -> SubmissionDraft:
    """アップロードを下書きに足し、remove の添付（この下書きのもの）を取り消す。"""
    draft = draft_for(worker, set_id)
    if draft is None:
        draft = SubmissionDraft.objects.create(
            worker=worker, set_id=set_id, created_by=by, updated_by=by
        )
    removing = set(remove)
    for item in draft.files.filter(
        attachment_id__in=removing, attachment__voided_at__isnull=True
    ).select_related("attachment"):
        attachments.void_attachment(
            item.attachment, by=by, reason=_("Removed on the screen of the submission PDF.")
        )
    for row_id, files in uploads.items():
        for file in files:
            try:
                attachment = attachments.create_attachment(
                    target=draft, kind=AttachmentKind.SUBMISSION_UPLOAD, file=file, created_by=by
                )
            except ValidationError as error:
                raise ValidationError({f"upload-{row_id}": error.messages}) from error
            SubmissionDraftFile.objects.create(
                draft=draft, row_id=row_id, attachment=attachment, created_by=by, updated_by=by
            )
    return draft
