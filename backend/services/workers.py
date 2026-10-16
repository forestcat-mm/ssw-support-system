"""外国人台帳の業務ロジック（番号の一意性・履歴の取り消し・確定値の確認・番号での検索）。"""

from __future__ import annotations

from datetime import date

from django.core.exceptions import ValidationError
from django.db.models import Model
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.models import User
from apps.workers.models import ConfirmedValue, HistoryRecord, PassportRecord, ResidenceRecord
from services import crypto


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
