"""相談・苦情対応（第5章 F05、T20）。受付・経過の追記・案件の取消・集計。

- 受付は支援実績の 1 行（種別「相談・苦情対応」、相手は本人に固定）＋案件 Consultation。
- 主な分類は 1 つ（複数にまたがる場合は主なもの）。定期届出の分類別の件数の合計を
  相談件数と一致させるため。
- 対応状況・引継ぎ先・通報・ハローワーク相談は経過の追記で変える。経過は変更・削除しない
  （誤りは「訂正」の経過を足す）。取消は案件ごと（理由必須）。
- 方法はメッセージ（メール・SNS 等）も選べる（相談・苦情だけ）。
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from django.core.exceptions import ValidationError
from django.core.files import File
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.models import Staff, User
from apps.support.models import (
    BodyLanguage,
    Consultation,
    ConsultationCategory,
    ConsultationEntry,
    ConsultationStatus,
    Counterpart,
    RecordMethod,
    ReportDestination,
)
from apps.workers.models import ForeignWorker
from services import incident_reports, support_records, workers
from services.support_catalog import SupportItem
from services.support_records import RecordInput

CONSULTATION_METHODS: tuple[str, ...] = (
    RecordMethod.IN_PERSON,
    RecordMethod.ONLINE,
    RecordMethod.PHONE,
    RecordMethod.MESSAGE,
)
OPEN_STATUSES: tuple[str, ...] = (ConsultationStatus.RECEIVED, ConsultationStatus.IN_PROGRESS)


@dataclass
class ReceptionInput:
    record: RecordInput  # 種別と相手はここで固定する
    category: str
    category_note: str = ""
    status: str = ConsultationStatus.RECEIVED
    handed_over_to: str = ""
    interpreter: str = ""  # 帳簿④ⅶ の通訳人（任意。T44）


@dataclass
class EntryInput:
    occurred_on: datetime.date
    method: str
    staff: Staff | None
    body: str
    status_after: str
    body_language: str = BodyLanguage.JA
    start_time: datetime.time | None = None
    handed_over_to: str = ""
    reported: bool = False
    report_to: str = ""
    report_to_note: str = ""
    reported_on: datetime.date | None = None
    hello_work: bool = False
    hello_work_on: datetime.date | None = None


class _Errors:
    def __init__(self) -> None:
        self.items: dict[str, list[ValidationError]] = {}

    def add(self, key: str, message: str, code: str) -> None:
        self.items.setdefault(key, []).append(ValidationError(message, code=code))

    def merge(self, error: ValidationError) -> None:
        for key, messages in error.error_dict.items():
            self.items.setdefault(key, []).extend(messages)

    def raise_if_any(self) -> None:
        if self.items:
            raise ValidationError(self.items)


def _check_status(errors: _Errors, status: str, handed_over_to: str) -> None:
    if status not in ConsultationStatus.values:
        errors.add("status", _("Select a valid choice."), "invalid_choice")
    elif status == ConsultationStatus.HANDED_OVER and not handed_over_to.strip():
        errors.add(
            "handed_over_to", _("Enter the organization the case was handed over to."), "required"
        )
    elif status != ConsultationStatus.HANDED_OVER and handed_over_to.strip():
        errors.add(
            "handed_over_to",
            _("Enter this only when the case is handed over to another organization."),
            "handed_over_to",
        )


def _reception_record(data: ReceptionInput) -> RecordInput:
    return replace(
        data.record,
        kind=SupportItem.CONSULTATION,
        counterpart=Counterpart.WORKER,
        counterpart_name="",
    )


def validate_reception(
    worker: ForeignWorker, data: ReceptionInput, *, today: datetime.date
) -> RecordInput:
    errors = _Errors()
    record = _reception_record(data)
    try:
        support_records.validate(
            worker,
            record,
            today=today,
            kinds=(SupportItem.CONSULTATION,),
            methods=CONSULTATION_METHODS,
        )
    except ValidationError as error:
        errors.merge(error)
    if data.category not in ConsultationCategory.values:
        errors.add("category", _("Select a valid choice."), "invalid_choice")
    elif data.category == ConsultationCategory.OTHER and not data.category_note.strip():
        errors.add("category_note", _("Enter the category."), "required")
    _check_status(errors, data.status, data.handed_over_to)
    errors.raise_if_any()
    return record


def create_consultation(
    worker: ForeignWorker,
    data: ReceptionInput,
    *,
    by: User | None,
    files: Iterable[File[Any]] = (),
    today: datetime.date | None = None,
) -> Consultation:
    """相談を受け付ける（支援実績の 1 行と案件を作る）。"""
    today = today or timezone.localdate()
    record_input = validate_reception(worker, data, today=today)
    uploads = support_records.validate_files(files)
    with transaction.atomic():
        record = support_records.save_record(worker, record_input, {}, uploads, by=by)
        consultation = Consultation(
            record=record,
            worker=worker,
            category=data.category,
            category_note=data.category_note.strip()
            if data.category == ConsultationCategory.OTHER
            else "",
            status=data.status,
            initial_status=data.status,
            handed_over_to=data.handed_over_to.strip(),
            interpreter=data.interpreter.strip()[:100],
            created_by=by,
            updated_by=by,
        )
        consultation.save()
    return consultation


def validate_entry(consultation: Consultation, data: EntryInput, *, today: datetime.date) -> None:
    errors = _Errors()
    if consultation.voided_at is not None:
        errors.add("__all__", _("This consultation is voided."), "voided")
    if not workers.is_active(consultation.worker):
        errors.add("__all__", _("This foreign worker is deactivated."), "deactivated")
    if data.occurred_on > today:
        errors.add("occurred_on", _("The date cannot be in the future."), "future")
    elif data.occurred_on < consultation.record.occurred_on:
        errors.add(
            "occurred_on", _("The date cannot be before the consultation was received."), "early"
        )
    if data.method not in CONSULTATION_METHODS:
        errors.add("method", _("Select a valid choice."), "invalid_choice")
    if data.staff is None:
        errors.add("staff", _("This field is required."), "required")
    elif not data.staff.is_active:
        errors.add("staff", _("This staff member is no longer active."), "inactive_staff")
    if not data.body.strip():
        errors.add("body", _("This field is required."), "required")
    if data.body_language not in BodyLanguage.values:
        errors.add("body_language", _("Select a valid choice."), "invalid_choice")
    _check_status(errors, data.status_after, data.handed_over_to)
    if data.reported:
        if data.report_to not in ReportDestination.values:
            errors.add("report_to", _("Select where to report."), "required")
        elif data.report_to == ReportDestination.OTHER and not data.report_to_note.strip():
            errors.add("report_to_note", _("Enter where to report."), "required")
    elif data.report_to or data.report_to_note.strip() or data.reported_on:
        errors.add("reported", _('Check "reported" to enter the report.'), "report_without_flag")
    if not data.hello_work and data.hello_work_on:
        errors.add(
            "hello_work",
            _('Check "consulted Hello Work" to enter the date.'),
            "hello_work_without_flag",
        )
    for name, value in (("reported_on", data.reported_on), ("hello_work_on", data.hello_work_on)):
        if value is not None and value > today:
            errors.add(name, _("The date cannot be in the future."), "future")
    errors.raise_if_any()


def add_entry(
    consultation: Consultation,
    data: EntryInput,
    *,
    by: User | None,
    today: datetime.date | None = None,
) -> ConsultationEntry:
    """経過を追記し、案件の状態・引継ぎ先・通報とハローワーク相談の有無を写す。"""
    today = today or timezone.localdate()
    validate_entry(consultation, data, today=today)
    with transaction.atomic():
        entry = ConsultationEntry(
            consultation=consultation,
            occurred_on=data.occurred_on,
            start_time=data.start_time,
            method=data.method,
            staff=data.staff,
            body=data.body.strip(),
            body_language=data.body_language,
            status_after=data.status_after,
            handed_over_to=data.handed_over_to.strip(),
            reported=data.reported,
            report_to=data.report_to if data.reported else "",
            report_to_note=data.report_to_note.strip() if data.reported else "",
            reported_on=data.reported_on if data.reported else None,
            hello_work=data.hello_work,
            hello_work_on=data.hello_work_on if data.hello_work else None,
            created_by=by,
            updated_by=by,
        )
        entry.save()
        consultation.status = data.status_after
        consultation.handed_over_to = data.handed_over_to.strip()
        consultation.reported = consultation.reported or data.reported
        consultation.hello_work = consultation.hello_work or data.hello_work
        consultation.updated_by = by
        consultation.save(
            update_fields=[
                "status",
                "handed_over_to",
                "reported",
                "hello_work",
                "updated_by",
                "updated_at",
            ]
        )
        # 通報・ハローワーク相談なら第4-3号（特異事案報告）のタスクを起票する（T24a）
        incident_reports.raise_tasks(incident_reports.of_entry(entry), by=by)
    return entry


def void_consultation(
    consultation: Consultation, *, by: User | None, reason: str
) -> incident_reports.Withdrawn:
    """案件を取り消す（受付の支援実績の行も）。経過は残る。

    経過から起票した第4-3号のタスクの参照を外す（T24a）。
    """
    if consultation.voided_at is not None:
        raise ValidationError(_("This consultation is already voided."), code="voided")
    if not reason.strip():
        raise ValidationError(_("Enter the reason for voiding."), code="void_reason")
    if not workers.is_active(consultation.worker):
        raise ValidationError(_("This foreign worker is deactivated."), code="deactivated")
    with transaction.atomic():
        consultation.voided_at = timezone.now()
        consultation.voided_by = by
        consultation.void_reason = reason.strip()
        consultation.updated_by = by
        consultation.save(
            update_fields=["voided_at", "voided_by", "void_reason", "updated_by", "updated_at"]
        )
        if consultation.record.voided_at is None:
            support_records.void_record(consultation.record, by=by, reason=reason, as_part=True)
        keys = [
            key
            for entry in consultation.entries.all()
            for key in (
                incident_reports.entry_report_key(entry),
                incident_reports.entry_hello_work_key(entry),
            )
        ]
        return incident_reports.withdraw(keys, by=by, reason=reason.strip())


# --- 一覧・表示 -------------------------------------------------------------------------


def consultations() -> models.QuerySet[Consultation]:
    return Consultation.objects.select_related(
        "record", "record__staff", "worker", "voided_by"
    ).order_by("-record__occurred_on", "-pk")


def filter_consultations(
    queryset: models.QuerySet[Consultation],
    *,
    statuses: Iterable[str] = (),
    category: str = "",
    staff_id: int | None = None,
    date_from: datetime.date | None = None,
    date_to: datetime.date | None = None,
    include_voided: bool = False,
) -> models.QuerySet[Consultation]:
    statuses = tuple(statuses)
    if not include_voided:
        queryset = queryset.filter(voided_at__isnull=True)
    if statuses:
        queryset = queryset.filter(status__in=statuses)
    if category:
        queryset = queryset.filter(category=category)
    if staff_id:
        queryset = queryset.filter(record__staff_id=staff_id)
    if date_from:
        queryset = queryset.filter(record__occurred_on__gte=date_from)
    if date_to:
        queryset = queryset.filter(record__occurred_on__lte=date_to)
    return queryset


@dataclass(frozen=True)
class TimelineItem:
    """経過の時系列の 1 行（受付または追記）。"""

    occurred_on: datetime.date
    start_time: datetime.time | None
    kind: str  # "reception" / "entry"
    status: str
    staff: Staff
    method_label: str
    body: str
    body_language: str
    entry: ConsultationEntry | None = None

    @property
    def status_label(self) -> str:
        return str(ConsultationStatus(self.status).label) if self.status else ""


def timeline(consultation: Consultation) -> list[TimelineItem]:
    """受付と経過を時系列に並べる（同じ日は時刻、無ければ入力順）。"""
    record = consultation.record
    items = [
        TimelineItem(
            occurred_on=record.occurred_on,
            start_time=record.start_time,
            kind="reception",
            status=consultation.initial_status,
            staff=record.staff,
            method_label=record.get_method_display(),
            body=record.body,
            body_language=record.body_language,
        )
    ]
    for entry in consultation.entries.select_related("staff").order_by("pk"):
        items.append(
            TimelineItem(
                occurred_on=entry.occurred_on,
                start_time=entry.start_time,
                kind="entry",
                status=entry.status_after,
                staff=entry.staff,
                method_label=entry.get_method_display(),
                body=entry.body,
                body_language=entry.body_language,
                entry=entry,
            )
        )
    # 受付が先頭。経過は日付順（同じ日は入力順）
    return [items[0], *sorted(items[1:], key=lambda item: item.occurred_on)]


# --- 集計（T24 の定期届出の材料） ----------------------------------------------------------


@dataclass
class ConsultationSummary:
    total: int = 0
    by_category: dict[str, int] = field(default_factory=dict)
    reported: int = 0
    hello_work: int = 0
    open: int = 0


def summary(
    worker_ids: Iterable[int], date_from: datetime.date, date_to: datetime.date
) -> dict[int, ConsultationSummary]:
    """外国人ごとの、期間内（受付日、両端を含む）に受け付けた相談の件数。取り消した案件は数えない。

    by_category の合計は total と一致する（主な分類は 1 件に 1 つ）。
    """
    result: dict[int, ConsultationSummary] = {}
    rows = filter_consultations(
        Consultation.objects.filter(worker_id__in=list(worker_ids)),
        date_from=date_from,
        date_to=date_to,
    ).values_list("worker_id", "category", "reported", "hello_work", "status")
    for worker_id, category, reported, hello_work, status in rows:
        item = result.setdefault(worker_id, ConsultationSummary())
        item.total += 1
        item.by_category[category] = item.by_category.get(category, 0) + 1
        item.reported += int(reported)
        item.hello_work += int(hello_work)
        item.open += int(status in OPEN_STATUSES)
    return result
