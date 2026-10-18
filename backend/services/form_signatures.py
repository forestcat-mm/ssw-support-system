"""生成した帳票の署名済みのスキャンと、署名済み書類の回収（仕様書 第6章「署名を要する書類の扱い」。
T33、ADR-0022）。

- 署名の状態は列で持たず、取り消していない添付の紐づけ（GeneratedDocumentSignature）から決める。
  生成物（完了の後は変えない）にも、紐づけの行（追記だけ）にも上書きは無い。
- 添付の置き場所（T33-D1）: 確認書（第5-8号・第5-9号）は記録の確認書の添付
  （briefings.add_confirmation。
  T21 の「署名済み」の判定がそのまま使える）。記録の無い様式（第1-17号など）は生成物の添付
  （種別「署名済みの帳票」）。署名日は紐づけの行に持つ（帳票タブはこの日を出す）。
- 回収のタスク（T33-D3・D4）: 様式の manifest の「署名済み添付が必須」が true のものだけ。
  外国人×様式×
  対象ごとに最新の完了した生成物について、生成から自機関の設定の日数がたっても署名済みが無ければ
  日次の起票で作る。署名済みになれば自動で完了、新しい生成物ができれば「不要」で閉じる。
  署名の添付を取り消すと未着手に戻る。元の記録（ガイダンスの記録）を取り消すと、共通の規則で戻す。
"""

from __future__ import annotations

import datetime
import enum
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.files import File
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.models import Attachment, AttachmentKind, Organization, User
from apps.filings.models import Filing, FilingKind, FilingStatus, Trigger
from apps.forms_engine.models import (
    GeneratedDocument,
    GeneratedDocumentSignature,
    GenerationStatus,
)
from apps.support.models import Briefing
from apps.workers.models import ForeignWorker
from services import attachments, briefings, deadlines, deliveries, filings, workers
from services.support_catalog import SupportItem

DEFAULT_DUE_DAYS = 14


class SignatureState(enum.StrEnum):
    NOT_REQUIRED = "not_required"  # 様式が署名済みの添付を求めない
    NOT_GENERATED = "not_generated"  # 依頼中・失敗（生成物が無い）
    AWAITING = "awaiting"  # 署名待ち
    SIGNED = "signed"  # 署名済み


def live_signatures(document: GeneratedDocument) -> models.QuerySet[GeneratedDocumentSignature]:
    return document.signatures.filter(attachment__voided_at__isnull=True).select_related(
        "attachment"
    )


def state(document: GeneratedDocument) -> SignatureState:
    if document.status != GenerationStatus.COMPLETED:
        return SignatureState.NOT_GENERATED
    if not document.template.requires_signed_copy:
        return SignatureState.NOT_REQUIRED
    return SignatureState.SIGNED if live_signatures(document).exists() else SignatureState.AWAITING


def signed_on(document: GeneratedDocument) -> datetime.date | None:
    """署名日（取り消していない紐づけのうち最も新しい日）。"""
    latest = live_signatures(document).order_by("-signed_on", "-pk").first()
    return latest.signed_on if latest else None


def due_days() -> int:
    organization = Organization.objects.first()
    return organization.signed_copy_due_days if organization else DEFAULT_DUE_DAYS


def deadline(generated_on: datetime.date) -> deadlines.Deadline:
    return deadlines.signed_copy(generated_on, due_days())


# --- 署名済みの添付 ---------------------------------------------------------------------


def can_add_signature(document: GeneratedDocument) -> bool:
    return (
        document.status == GenerationStatus.COMPLETED
        and document.template.requires_signed_copy
        and document.worker is not None
        and workers.is_active(document.worker)
        and not (isinstance(document.target, Briefing) and document.target.record.voided_at)
    )


def add_signature(
    document: GeneratedDocument,
    files: Iterable[File[Any]],
    *,
    signed_on: datetime.date | None,
    by: User | None,
    today: datetime.date | None = None,
) -> list[GeneratedDocumentSignature]:
    """署名済みのスキャンを添付し、生成物に紐づける（署名日は必須。生成日より前・未来は不可）。"""
    today = today or timezone.localdate()
    if not can_add_signature(document):
        raise ValidationError(_("A signed copy cannot be added to this document."), code="locked")
    files = list(files)
    errors: dict[str, list[ValidationError]] = {}
    if not files:
        errors["files"] = [ValidationError(_("Select a file."), code="required")]
    generated_on = timezone.localdate(document.generated_at) if document.generated_at else today
    if signed_on is None:
        errors["signed_on"] = [
            ValidationError(_("Enter the date of the signature."), code="required")
        ]
    elif signed_on > today:
        errors["signed_on"] = [
            ValidationError(_("The date cannot be in the future."), code="future")
        ]
    elif signed_on < generated_on:
        errors["signed_on"] = [
            ValidationError(
                _("The date of the signature cannot be before the form was generated (%(day)s).")
                % {"day": generated_on.isoformat()},
                code="before_generation",
            )
        ]
    if errors:
        raise ValidationError(errors)
    assert signed_on is not None
    target = document.target
    with transaction.atomic():
        if isinstance(target, Briefing):
            uploaded = briefings.add_confirmation(
                target, files, signed_on=signed_on, by=by, today=today
            )
        else:
            for file in files:
                attachments.validate_upload(AttachmentKind.SIGNED_DOCUMENT, file)
            uploaded = [
                attachments.create_attachment(
                    target=document, kind=AttachmentKind.SIGNED_DOCUMENT, file=file, created_by=by
                )
                for file in files
            ]
        rows = [
            GeneratedDocumentSignature.objects.create(
                document=document,
                attachment=attachment,
                signed_on=signed_on,
                created_by=by,
                updated_by=by,
            )
            for attachment in uploaded
        ]
        for filing in _unfinished_tasks(document):
            filings.complete_by_record(
                filing, by=by, reason=_("The signed copy was attached."), source=rows[0]
            )
    return rows


def on_attachment_voided(attachment: Attachment, *, by: User | None) -> None:
    """署名の添付を取り消したとき、署名待ちに戻れば、自動で完了した回収のタスクを未着手に戻す。"""
    signature = GeneratedDocumentSignature.objects.filter(attachment=attachment).first()
    if signature is None:
        return
    document = signature.document
    if live_signatures(document).exists():
        return
    for filing in _tasks_of(document).filter(status=FilingStatus.COMPLETED):
        filings.reopen_by_record(
            filing, by=by, reason=_("The signed copy was voided."), source=signature
        )


def on_briefing_voided(briefing: Briefing, *, by: User | None, reason: str) -> None:
    """元の記録（ガイダンス・オリエンテーション）を取り消したときの回収のタスク（共通の規則）。"""
    content_type = ContentType.objects.get_for_model(Briefing)
    for document in GeneratedDocument.objects.filter(
        content_type=content_type, object_id=briefing.pk
    ):
        for filing in _unfinished_tasks(document):
            filings.revert_raised_task(
                filing, source="briefing", reason=reason, by=by, by_record=True
            )


# --- 回収のタスク -----------------------------------------------------------------------


def _tasks_of(document: GeneratedDocument) -> models.QuerySet[Filing]:
    return Filing.objects.filter(
        kind=FilingKind.SIGNED_COPY_COLLECTION,
        content_type=ContentType.objects.get_for_model(GeneratedDocument),
        object_id=document.pk,
    )


def _unfinished_tasks(document: GeneratedDocument) -> list[Filing]:
    return list(_tasks_of(document).filter(status__in=list(filings.OPEN_STATUSES)))


def _key(document: GeneratedDocument) -> tuple[Any, ...]:
    return (
        document.worker_id,
        document.template.form_id,
        document.content_type_id,
        document.object_id,
    )


def latest_documents() -> dict[tuple[Any, ...], GeneratedDocument]:
    """外国人×様式×対象ごとの、最新の完了した生成物（署名済みの添付が必須の様式だけ）。"""
    result: dict[tuple[Any, ...], GeneratedDocument] = {}
    documents = (
        GeneratedDocument.objects.filter(
            status=GenerationStatus.COMPLETED, template__requires_signed_copy=True
        )
        .select_related("template", "worker")
        .order_by("generated_at", "pk")
    )
    for document in documents:
        result[_key(document)] = document
    return result


def raise_collection_tasks(today: datetime.date, *, by: User | None = None) -> list[Filing]:
    """日次の起票（filings.create_scheduled から）。古い生成物の回収のタスクは閉じる。"""
    created: list[Filing] = []
    latest = latest_documents()
    latest_ids = {document.pk for document in latest.values()}
    stale = Filing.objects.filter(
        kind=FilingKind.SIGNED_COPY_COLLECTION, status__in=list(filings.OPEN_STATUSES)
    ).exclude(object_id__in=latest_ids)
    for filing in stale:
        filings.close_not_required(
            filing, by=by, reason=_("A newer document was generated."), source=filing
        )
    for document in latest.values():
        if document.worker is None or not workers.is_active(document.worker):
            continue
        if state(document) is not SignatureState.AWAITING or document.generated_at is None:
            continue
        generated_on = timezone.localdate(document.generated_at)
        task_deadline = deadline(generated_on)
        if task_deadline.opens_on is not None and today < task_deadline.opens_on:
            continue
        raised, was_created = filings.create_if_active(
            FilingKind.SIGNED_COPY_COLLECTION,
            target=document,
            basis_on=generated_on,
            trigger=Trigger.FORM_GENERATED,
            by=by,
            deadline=task_deadline,
            details={"form_id": document.template.form_id},
        )
        if was_created and raised is not None:
            created.append(raised)
    return created


# --- 帳票タブの行（T33）---------------------------------------------------------------


@dataclass(frozen=True)
class DocumentRow:
    document: GeneratedDocument
    state: SignatureState
    signed_on: datetime.date | None
    scans: list[Attachment]  # 取り消していない署名済みのスキャン
    stale: bool  # 依頼のまま時間がかかっている


@dataclass(frozen=True)
class LegacyRow:
    """生成履歴の無い、既存の署名済みの書類（T21 の確認書・T22 の控え）。"""

    label: str
    signed_on: datetime.date | None
    url: str


def rows_for(worker: ForeignWorker) -> list[DocumentRow]:
    from services import form_generation  # 循環を避ける

    documents = (
        GeneratedDocument.objects.filter(worker=worker)
        .select_related("template", "created_by")
        .order_by("-created_at", "-pk")
    )
    rows = []
    for document in documents:
        signatures = list(live_signatures(document).order_by("-signed_on", "-pk"))
        rows.append(
            DocumentRow(
                document=document,
                state=state(document),
                signed_on=signatures[0].signed_on if signatures else None,
                scans=[signature.attachment for signature in signatures],
                stale=form_generation.is_stale(document),
            )
        )
    return rows


def legacy_rows(worker: ForeignWorker) -> list[LegacyRow]:
    """生成履歴に紐づかない署名済みの確認書（T21）と、署名済みの控えのある交付記録（T22）。"""
    from django.urls import reverse  # 循環を避ける

    linked = set(GeneratedDocumentSignature.objects.values_list("attachment_id", flat=True))
    rows: list[LegacyRow] = []
    for briefing in briefings.briefings_of(worker).filter(record__voided_at__isnull=True):
        files = [
            file
            for file in briefing.record.attachments.all()
            if file.voided_at is None and file.pk not in linked
        ]
        if briefings.is_signed(briefing, files):
            rows.append(
                LegacyRow(
                    label=str(
                        briefings.CONFIRMATION_KINDS[SupportItem(briefing.record.kind)].label
                    ),
                    signed_on=briefing.signed_on,
                    url=reverse("workers:support_briefing", args=[worker.pk, briefing.pk]),
                )
            )
    for delivery in deliveries.deliveries_of(worker).filter(
        voided_at__isnull=True, generated_document__isnull=True, kind__in=deliveries.RECEIPT_KINDS
    ):
        if deliveries.has_receipt(delivery):
            rows.append(
                LegacyRow(
                    label=str(delivery.get_kind_display()),
                    signed_on=delivery.delivered_on,
                    url=reverse("workers:support_delivery", args=[worker.pk, delivery.pk]),
                )
            )
    return rows
