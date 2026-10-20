"""外国人の担当者・事務所の支援責任者の履歴（T45。ADR-0037）。

履歴の行（StaffAssignment・OfficeManagerPeriod）は上書きしない。誤りは取消（理由は必須）して
新しい行を足す。今の値（ForeignWorker.assigned_staff・Office.support_manager）は、有効な行の
いちばん新しい開始日の行の写しで、同じトランザクションで合わせる。
"""

import datetime
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.models import Office, OfficeManagerPeriod, Staff, StaffPeriod, User
from apps.workers.models import ForeignWorker, StaffAssignment
from services import organization

REASON_MAX_LENGTH = 200


@dataclass(frozen=True)
class StaffOn:
    """ある日の担当者。estimated は、推定の行を使ったか、履歴の外（最初の行より前・行が無い）。"""

    staff: Staff | None
    estimated: bool


def _stamp(instance: Any, by: User | None) -> None:
    if instance.pk is None:
        instance.created_by = by
    instance.updated_by = by


def worker_periods(worker: ForeignWorker) -> list[StaffAssignment]:
    """担当者の履歴（取消した行も。開始日の新しい順）。"""
    return list(worker.staff_assignments.select_related("staff", "voided_by"))


def office_periods(office: Office) -> list[OfficeManagerPeriod]:
    """支援責任者の履歴（取消した行も。開始日の新しい順）。"""
    return list(office.manager_periods.select_related("staff", "voided_by"))


def staff_on(
    periods: list[StaffAssignment] | list[OfficeManagerPeriod],
    day: datetime.date,
    *,
    fallback: Staff | None = None,
) -> StaffOn:
    """day の時点の担当者。行が無ければ今の値（fallback）を推定として返す。

    day が最初の行より前なら、最初の行の担当者を推定として返す（台帳に載せる前の期間）。
    """
    active = sorted(
        (period for period in periods if period.voided_at is None),
        key=lambda period: (period.starts_on, period.pk),
    )
    if not active:
        return StaffOn(fallback, fallback is not None)
    applied = [period for period in active if period.starts_on <= day]
    if not applied:
        return StaffOn(active[0].staff, True)
    return StaffOn(applied[-1].staff, applied[-1].is_estimated)


def worker_staff_on(worker: ForeignWorker, day: datetime.date) -> StaffOn:
    return staff_on(worker_periods(worker), day, fallback=worker.assigned_staff)


def periods_by_worker(worker_ids: list[int]) -> dict[int, list[StaffAssignment]]:
    """外国人ごとの履歴（定期届出の元データなど、まとめて引くとき）。"""
    result: dict[int, list[StaffAssignment]] = {}
    for period in StaffAssignment.objects.filter(worker_id__in=worker_ids).select_related(
        "staff__office"
    ):
        result.setdefault(period.worker_id, []).append(period)
    return result


def _active(periods: list[Any]) -> list[Any]:
    return sorted(
        (period for period in periods if period.voided_at is None),
        key=lambda period: (period.starts_on, period.pk),
    )


def _validate(
    periods: list[Any],
    current: Staff | None,
    staff: Staff | None,
    *,
    starts_on: datetime.date,
    reason: str,
    today: datetime.date,
) -> None:
    errors: dict[str, str] = {}
    if staff == current:
        errors["staff"] = _("Choose a different staff member from the current one.")
    if starts_on > today:
        errors["starts_on"] = _("The start date cannot be in the future.")
    active = _active(periods)
    if active and starts_on <= active[-1].starts_on:
        errors["starts_on"] = _(
            "Enter a date after %(day)s (the start of the current one). To correct the current "
            "one, void it first."
        ) % {"day": active[-1].starts_on.isoformat()}
    if not reason.strip():
        errors["reason"] = _("Enter the reason for the change.")
    if errors:
        raise ValidationError(errors)


def _close(period: StaffPeriod, *, by: User | None, reason: str) -> None:
    if not reason.strip():
        raise ValidationError(_("Enter the reason for voiding."), code="void_reason")
    period.voided_at = timezone.now()
    period.voided_by = by
    period.void_reason = reason.strip()[:REASON_MAX_LENGTH]
    period.updated_by = by
    period.save(update_fields=["voided_at", "voided_by", "void_reason", "updated_by", "updated_at"])


# --- 外国人の担当者 -----------------------------------------------------------------------


def start_worker(
    worker: ForeignWorker, *, starts_on: datetime.date, by: User | None
) -> StaffAssignment | None:
    """登録のときの最初の行（担当者がいるときだけ）。"""
    if worker.assigned_staff_id is None:
        return None
    period = StaffAssignment(worker=worker, staff=worker.assigned_staff, starts_on=starts_on)
    _stamp(period, by)
    period.save()
    return period


def _sync_worker(worker: ForeignWorker, *, by: User | None) -> None:
    active = _active(worker_periods(worker))
    staff = active[-1].staff if active else None
    if worker.assigned_staff_id != (staff.pk if staff else None):
        worker.assigned_staff = staff
        worker.updated_by = by
        worker.save(update_fields=["assigned_staff", "updated_by", "updated_at"])


@transaction.atomic
def change_worker_staff(
    worker: ForeignWorker,
    staff: Staff | None,
    *,
    starts_on: datetime.date,
    reason: str,
    by: User | None,
    today: datetime.date,
) -> StaffAssignment:
    """担当者を変える（T45-D4）。新しい行を足し、今の担当者の写しを合わせる。"""
    ForeignWorker.objects.select_for_update().filter(pk=worker.pk).first()
    worker.refresh_from_db()
    _validate(
        worker_periods(worker),
        worker.assigned_staff,
        staff,
        starts_on=starts_on,
        reason=reason,
        today=today,
    )
    period = StaffAssignment(
        worker=worker, staff=staff, starts_on=starts_on, reason=reason.strip()[:REASON_MAX_LENGTH]
    )
    _stamp(period, by)
    period.save()
    _sync_worker(worker, by=by)
    return period


@transaction.atomic
def void_worker_period(period: StaffAssignment, *, by: User | None, reason: str) -> None:
    """履歴の行を取り消す。今の担当者は、残った行のいちばん新しいものに戻る。"""
    if period.voided_at is not None:
        return
    _close(period, by=by, reason=reason)
    _sync_worker(period.worker, by=by)


# --- 事務所の支援責任者 -------------------------------------------------------------------


def _sync_office(office: Office, *, by: User | None) -> None:
    active = _active(office_periods(office))
    staff = active[-1].staff if active else None
    if office.support_manager_id != (staff.pk if staff else None):
        office.support_manager = staff
        office.updated_by = by
        office.save(update_fields=["support_manager", "updated_by", "updated_at"])


@transaction.atomic
def change_office_manager(
    office: Office,
    staff: Staff | None,
    *,
    starts_on: datetime.date,
    reason: str,
    by: User | None,
    today: datetime.date,
) -> OfficeManagerPeriod:
    """事務所の支援責任者を変える（T45-D5）。"""
    Office.objects.select_for_update().filter(pk=office.pk).first()
    office.refresh_from_db()
    try:
        organization.validate_support_manager(office, staff)
    except ValidationError as error:
        raise ValidationError({"staff": error.messages}) from error
    _validate(
        office_periods(office),
        office.support_manager,
        staff,
        starts_on=starts_on,
        reason=reason,
        today=today,
    )
    period = OfficeManagerPeriod(
        office=office, staff=staff, starts_on=starts_on, reason=reason.strip()[:REASON_MAX_LENGTH]
    )
    _stamp(period, by)
    period.save()
    _sync_office(office, by=by)
    return period


@transaction.atomic
def void_office_period(period: OfficeManagerPeriod, *, by: User | None, reason: str) -> None:
    if period.voided_at is not None:
        return
    _close(period, by=by, reason=reason)
    _sync_office(period.office, by=by)
