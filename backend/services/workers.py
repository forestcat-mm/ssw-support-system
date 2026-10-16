"""外国人台帳の業務ロジック（入力の検査・登録・履歴の追加と取り消し・確定値の確認・番号での検索）。

入力の検査は admin と業務画面（T13d）で共通。フォームは項目の型の検査だけを行い、
業務のルール（正規化・形式・別人への同じ番号・期間・入国前のカードなし）はここで判定する。
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Model, QuerySet
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.models import AuditAction, User
from apps.filings.models import Filing, FilingStatus
from apps.workers.models import (
    ConfirmedValue,
    ForeignWorker,
    HistoryRecord,
    PassportRecord,
    ResidenceRecord,
    WorkerStatus,
)
from services import audit, crypto, filings, passport, residence_card


def conflicting_worker_id(
    model: type[ResidenceRecord] | type[PassportRecord], number: str, worker_id: int | None
) -> int | None:
    """同じ番号（ブラインドインデックス）を持つ、取り消していない別の人の行があればその人の ID。

    DB の排他制約と同じ判定を保存前に行い、分かりやすいエラーにするために使う。
    """
    index = crypto.blind_index(number)
    if index is None:
        return None
    index_field = "card_number_index" if model is ResidenceRecord else "number_index"
    others = model._base_manager.filter(**{index_field: index}, voided_at__isnull=True)
    if worker_id is not None:
        others = others.exclude(worker_id=worker_id)
    return others.values_list("worker_id", flat=True).first()


def validate_number_not_taken(
    model: type[ResidenceRecord] | type[PassportRecord], number: str, worker_id: int | None
) -> None:
    other = conflicting_worker_id(model, number, worker_id)
    if other is None:
        return
    if model is ResidenceRecord:
        message = _(
            "This residence card number is registered to another foreign worker (ID: %(id)s)."
        )
    else:
        message = _("This passport number is registered to another foreign worker (ID: %(id)s).")
    raise ValidationError(message % {"id": other}, code="number_taken")


def validate_period(start: date | None, end: date | None) -> None:
    """開始日（許可日・発行日）より後に終了日（期限）があること。"""
    if start and end and end <= start:
        raise ValidationError(_("The expiry date must be after the start date."), code="period")


def clean_residence(
    values: Mapping[str, Any], *, worker_id: int | None, worker_status: str | None
) -> dict[str, Any]:
    """在留情報の入力の検査。正規化した在留カード番号を返す。エラーは項目ごとの ValidationError。

    在留カード番号を空にできるのは入国前の人だけ（在留中などの登録漏れを防ぐ）。
    """
    errors: dict[str, list[ValidationError]] = {}
    card_number = residence_card.normalize(str(values.get("card_number") or ""))
    if card_number:
        try:
            residence_card.validate(card_number)
            validate_number_not_taken(ResidenceRecord, card_number, worker_id)
        except ValidationError as error:
            errors["card_number"] = [error]
    elif worker_status != WorkerStatus.PRE_ENTRY:
        errors["card_number"] = [
            ValidationError(
                _("Enter the residence card number. Only workers before entry can leave it blank."),
                code="card_number_required",
            )
        ]
    try:
        validate_period(values.get("permitted_on"), values.get("expires_on"))
    except ValidationError as error:
        errors["expires_on"] = [error]
    if errors:
        raise ValidationError(errors)
    return {"card_number": card_number}


def clean_passport(values: Mapping[str, Any], *, worker_id: int | None) -> dict[str, Any]:
    """旅券の入力の検査。正規化した旅券番号を返す。"""
    errors: dict[str, list[ValidationError]] = {}
    number = passport.normalize(str(values.get("number") or ""))
    try:
        passport.validate(number)
        validate_number_not_taken(PassportRecord, number, worker_id)
    except ValidationError as error:
        errors["number"] = [error]
    try:
        validate_period(values.get("issued_on"), values.get("expires_on"))
    except ValidationError as error:
        errors["expires_on"] = [error]
    if errors:
        raise ValidationError(errors)
    return {"number": number}


def _stamp(instance: Model, by: User | None) -> None:
    """作成者・更新者と、確定値の最終確認者・確認日時（T07 の判断5）を入れる。"""
    if instance.pk is None:
        instance.created_by = by  # type: ignore[attr-defined]
    instance.updated_by = by  # type: ignore[attr-defined]
    confirm(instance, by=by)


def add_record(worker: ForeignWorker, record: HistoryRecord, *, by: User | None) -> HistoryRecord:
    """履歴行（在留・旅券・住所・連絡先）を 1 行足す。検査は呼び出し前に clean_* で済ませる。"""
    record.worker = worker  # type: ignore[attr-defined]
    _stamp(record, by)
    record.save()
    return record


@transaction.atomic
def register_worker(
    worker: ForeignWorker, records: Iterable[HistoryRecord], *, by: User | None
) -> ForeignWorker:
    """外国人を登録し、最初の在留情報・旅券・住所・連絡先（任意）を同じトランザクションで足す。"""
    _stamp(worker, by)
    worker.save()
    for record in records:
        add_record(worker, record, by=by)
    return worker


def update_worker(
    worker: ForeignWorker, *, by: User | None, loaded_updated_at: datetime.datetime | None
) -> ForeignWorker:
    """基本情報を保存する。編集画面を開いた後にほかの人が更新していたら上書きしない。"""
    current = (
        ForeignWorker.objects.filter(pk=worker.pk).values_list("updated_at", flat=True).first()
    )
    if loaded_updated_at is not None and current is not None and current != loaded_updated_at:
        raise ValidationError(
            _(
                "Someone else updated this foreign worker after you opened this page. "
                "Reload the page and enter your changes again."
            ),
            code="stale",
        )
    _stamp(worker, by)
    worker.save()
    return worker


def void_record(record: HistoryRecord, *, by: User | None, reason: str) -> None:
    """履歴行を取り消す（入力ミスの訂正。正しい行は別に足す）。最新行のビューから外れる。"""
    if record.voided_at is not None:
        return
    if not reason.strip():
        raise ValidationError(_("Enter the reason for voiding."), code="void_reason")
    record.voided_at = timezone.now()
    record.voided_by = by
    record.void_reason = reason.strip()
    record.updated_by = by
    record.save(update_fields=["voided_at", "voided_by", "void_reason", "updated_by", "updated_at"])


def confirm(instance: Model, *, by: User | None) -> None:
    """担当者が入力・確認した確定値として、最終確認者・確認日時を入れる（保存は呼び出し側）。"""
    if isinstance(instance, ConfirmedValue):
        instance.confirmed_by = by
        instance.confirmed_at = timezone.now()


def worker_ids_by_document_number(query: str) -> set[int]:
    """在留カード番号・旅券番号の完全一致（表記ゆれは吸収）で外国人を探す。取り消した行も含む。"""
    index = crypto.blind_index(query)
    if index is None:
        return set()
    residence = ResidenceRecord.objects.filter(card_number_index=index).values_list(
        "worker_id", flat=True
    )
    passports = PassportRecord.objects.filter(number_index=index).values_list(
        "worker_id", flat=True
    )
    return set(residence) | set(passports)


# --- 無効化（T13e）---------------------------------------------------------------


def active_workers() -> QuerySet[ForeignWorker]:
    """無効化していない外国人。一覧・検索・日次の起票・ダッシュボードは必ずこれを起点にする。"""
    return ForeignWorker.objects.filter(deactivated_at__isnull=True)


def is_active(worker: ForeignWorker) -> bool:
    return worker.deactivated_at is None


@dataclass(frozen=True)
class DeactivationEffects:
    """無効化で取り消す行と、取消にする届出タスク（確認画面で先に見せる）。"""

    residence_records: list[ResidenceRecord]
    passport_records: list[PassportRecord]
    filings: list[Any]


def deactivation_effects(worker: ForeignWorker) -> DeactivationEffects:
    return DeactivationEffects(
        residence_records=list(worker.residence_records.filter(voided_at__isnull=True)),
        passport_records=list(worker.passport_records.filter(voided_at__isnull=True)),
        filings=list(
            Filing.objects.filter(worker=worker, status__in=filings.OPEN_STATUSES).order_by(
                "unadjusted_due_on", "pk"
            )
        ),
    )


@transaction.atomic
def deactivate(worker: ForeignWorker, *, by: User | None, reason: str) -> DeactivationEffects:
    """外国人を無効化する。取り消していない在留情報・旅券を取り消し、未完了の届出タスクを取消にする。

    監査ログは「外国人の無効化」の 1 件にまとめる（中の取消・状態の変更はその変更前後に入れる）。
    """
    if worker.deactivated_at is not None:
        raise ValidationError(_("This foreign worker is already deactivated."), code="deactivated")
    if not reason.strip():
        raise ValidationError(_("Enter the reason for deactivation."), code="reason")
    effects = deactivation_effects(worker)
    cascade_reason = _("Deactivation of the foreign worker")
    with audit.event(AuditAction.WORKER_DEACTIVATE, target=worker, actor=by) as event:
        worker.deactivated_at = timezone.now()
        worker.deactivated_by = by
        worker.deactivation_reason = reason.strip()
        worker.updated_by = by
        worker.save(
            update_fields=[
                "deactivated_at",
                "deactivated_by",
                "deactivation_reason",
                "updated_by",
                "updated_at",
            ]
        )
        for record in [*effects.residence_records, *effects.passport_records]:
            void_record(record, by=by, reason=cascade_reason)
        for filing in effects.filings:
            filings.validate_transition(filing, FilingStatus.CANCELLED, reason=cascade_reason)
            event.changes.setdefault("cancelled_filings", []).append(
                [filing.pk, filing.kind, filing.status]
            )
            filing.status = FilingStatus.CANCELLED
            filing.status_reason = cascade_reason
            filing.updated_by = by
            filing.save(update_fields=["status", "status_reason", "updated_by", "updated_at"])
        event.changes["voided_residence_records"] = [r.pk for r in effects.residence_records]
        event.changes["voided_passport_records"] = [r.pk for r in effects.passport_records]
    return effects


@transaction.atomic
def reactivate(worker: ForeignWorker, *, by: User | None, reason: str) -> None:
    """無効化を取り消す（誤って無効化したとき。admin から管理者だけが行う）。

    無効化のときに取り消した在留情報・旅券と届出タスクは戻さない（新しい行として登録し直す）。
    監査ログには元の無効化の理由と、取り消しの理由の両方を残す。
    """
    if worker.deactivated_at is None:
        raise ValidationError(_("This foreign worker is not deactivated."), code="active")
    if not reason.strip():
        raise ValidationError(_("Enter the reason for cancelling the deactivation."), code="reason")
    context = {
        "deactivation_reason": worker.deactivation_reason,
        "reactivation_reason": reason.strip(),
    }
    with audit.event(AuditAction.WORKER_REACTIVATE, target=worker, actor=by, context=context):
        worker.deactivated_at = None
        worker.deactivated_by = None
        worker.deactivation_reason = ""
        worker.updated_by = by
        worker.save(
            update_fields=[
                "deactivated_at",
                "deactivated_by",
                "deactivation_reason",
                "updated_by",
                "updated_at",
            ]
        )
