"""定期面談の記録（第5章 F05・F06、T19）。本人用・上司用の 2 行を組で記録し、タスクを進める。

- 2 行とも必須。方法は対面かオンラインだけ（音声だけの電話は定期面談に使えない）。
- 本人用の行の相手は本人、上司用の行の相手は受入れ機関の上司（氏名は必須）に固定する。
- 記録すると、その外国人の開いている「定期面談」タスクを完了にし、次回のタスクを起票する
  （起算日は 2 行の実施日の早い方、期限は次回の予定日。無ければ法定の 3 か月後）。
- 取消は組ごと（理由必須）。完了させたタスクと次回のタスクに手が付いていなければ自動で戻す。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, replace
from typing import Any

from django.core.exceptions import ValidationError
from django.core.files import File
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.models import User
from apps.filings.models import Filing, FilingKind, FilingStatus, Trigger
from apps.support.models import (
    Counterpart,
    PeriodicInterview,
    RecordMethod,
    ReportDestination,
    SupportRecord,
)
from apps.workers.models import ForeignWorker
from services import deadlines, filings, support_records, workers
from services.support_catalog import SupportItem
from services.support_records import RecordInput

INTERVIEW_METHODS: tuple[str, ...] = (RecordMethod.IN_PERSON, RecordMethod.ONLINE)
ROWS = ("self", "supervisor")  # 本人用・上司用（エラーの接頭辞にも使う）
ROW_COUNTERPARTS = {"self": Counterpart.WORKER, "supervisor": Counterpart.SUPERVISOR}


@dataclass
class InterviewInput:
    """定期面談の組の入力。行の相手は ROW_COUNTERPARTS で上書きする。"""

    self_row: RecordInput
    supervisor_row: RecordInput
    violation_suspected: bool = False
    report_required: bool | None = None
    report_to: str = ""
    report_to_note: str = ""
    reported_on: datetime.date | None = None
    next_scheduled_on: datetime.date | None = None


@dataclass(frozen=True)
class VoidResult:
    tasks_reverted: bool  # タスクを自動で戻したか（False なら手で直す）


def _rows(data: InterviewInput) -> dict[str, RecordInput]:
    return {
        name: replace(row, kind=SupportItem.PERIODIC_INTERVIEW, counterpart=ROW_COUNTERPARTS[name])
        for name, row in (("self", data.self_row), ("supervisor", data.supervisor_row))
    }


def basis_of(rows: dict[str, RecordInput]) -> datetime.date:
    """次回の起算日（2 行の実施日の早い方。期限を短く見る側）。"""
    return min(row.occurred_on for row in rows.values())


def legal_due_on(basis_on: datetime.date) -> datetime.date:
    return deadlines.periodic_interview(basis_on).due_on


def validate(
    worker: ForeignWorker, data: InterviewInput, *, today: datetime.date
) -> dict[str, RecordInput]:
    """組を検査し、相手を固定した 2 行を返す。エラーは "self.body"・"next_scheduled_on" など。"""
    rows = _rows(data)
    errors: dict[str, list[ValidationError]] = {}

    def add(key: str, message: str, code: str) -> None:
        errors.setdefault(key, []).append(ValidationError(message, code=code))

    for name, row in rows.items():
        try:
            support_records.validate(
                worker,
                row,
                today=today,
                kinds=(SupportItem.PERIODIC_INTERVIEW,),
                methods=INTERVIEW_METHODS,
            )
        except ValidationError as error:
            for key, messages in error.error_dict.items():
                target = "__all__" if key == "__all__" else f"{name}.{key}"
                errors.setdefault(target, []).extend(messages)
        if name == "supervisor" and not row.counterpart_name.strip():
            add("supervisor.counterpart_name", _("Enter the name of the supervisor."), "required")

    if data.violation_suspected:
        if data.report_required is None:
            add("report_required", _("Select whether a report is required."), "required")
        if data.report_required:
            if data.report_to not in ReportDestination.values:
                add("report_to", _("Select where to report."), "required")
            elif data.report_to == ReportDestination.OTHER and not data.report_to_note.strip():
                add("report_to_note", _("Enter where to report."), "required")
    elif data.report_required is not None or data.report_to or data.reported_on:
        add(
            "report_required",
            _("Enter the report only when a violation of labour laws is suspected."),
            "report_without_violation",
        )
    if data.reported_on is not None and data.reported_on > today:
        add("reported_on", _("The date cannot be in the future."), "future")
    if data.reported_on is not None and not data.report_required:
        add("reported_on", _("Enter the date only when a report is required."), "reported_on")

    if data.next_scheduled_on is not None and "self.occurred_on" not in errors:
        basis = basis_of(rows)
        latest = max(row.occurred_on for row in rows.values())
        legal = legal_due_on(basis)
        if not latest < data.next_scheduled_on <= legal:
            add(
                "next_scheduled_on",
                _("Enter a date after the interview and on or before %(legal)s (3 months).")
                % {"legal": legal.isoformat()},
                "next_scheduled_on",
            )
    if errors:
        raise ValidationError(errors)
    return rows


def open_task(worker: ForeignWorker, preferred: Filing | None = None) -> Filing | None:
    """完了にする定期面談のタスク（指定があればそれ。無ければ期限の早い開いているもの）。"""
    queryset = Filing.objects.filter(
        worker=worker,
        kind=FilingKind.PERIODIC_INTERVIEW,
        status__in=list(filings.OPEN_STATUSES),
    )
    if preferred is not None:
        match = queryset.filter(pk=preferred.pk).first()
        if match is not None:
            return match
    return queryset.order_by("unadjusted_due_on", "pk").first()


def create_interview(
    worker: ForeignWorker,
    data: InterviewInput,
    *,
    by: User | None,
    files: dict[str, list[File[Any]]] | None = None,
    task: Filing | None = None,
    today: datetime.date | None = None,
) -> PeriodicInterview:
    """組を記録し、タスクを完了にして次回のタスクを起票する（1 つのトランザクション）。

    もっと新しい面談がすでに記録されているとき（過去の面談の後からの記録）はタスクを動かさない。
    """
    today = today or timezone.localdate()
    rows = validate(worker, data, today=today)
    uploads: dict[str, list[File[Any]]] = {}
    for name in ROWS:
        try:
            uploads[name] = support_records.validate_files((files or {}).get(name, []))
        except ValidationError as error:
            raise ValidationError({f"{name}.uploads": error.error_dict["files"]}) from error
    basis = basis_of(rows)
    with transaction.atomic():
        newer_exists = PeriodicInterview.objects.filter(
            worker=worker, voided_at__isnull=True, basis_on__gt=basis
        ).exists()
        interview = PeriodicInterview(
            worker=worker,
            basis_on=basis,
            violation_suspected=data.violation_suspected,
            report_required=data.report_required if data.violation_suspected else None,
            report_to=data.report_to if data.report_required else "",
            report_to_note=data.report_to_note.strip() if data.report_required else "",
            reported_on=data.reported_on if data.report_required else None,
            next_scheduled_on=data.next_scheduled_on,
            created_by=by,
            updated_by=by,
        )
        interview.save()
        for name in ROWS:
            support_records.save_record(
                worker, rows[name], {}, uploads[name], by=by, interview=interview
            )
        if not newer_exists:
            _advance_tasks(interview, task=task, by=by)
    return interview


def _advance_tasks(interview: PeriodicInterview, *, task: Filing | None, by: User | None) -> None:
    worker = interview.worker
    current = open_task(worker, task)
    if current is not None:
        filings.transition(current, FilingStatus.COMPLETED, by=by, by_record=True)
        interview.completed_filing = current
    next_filing, created = filings.create(
        FilingKind.PERIODIC_INTERVIEW,
        target=worker,
        basis_on=interview.basis_on,
        trigger=Trigger.INTERVIEW_RECORDED,
        by=by,
        details={
            "interview": interview.pk,
            "scheduled_on": interview.next_scheduled_on.isoformat()
            if interview.next_scheduled_on
            else None,
        },
        deadline=deadlines.periodic_interview(interview.basis_on, interview.next_scheduled_on),
    )
    if created:
        interview.next_filing = next_filing
    interview.updated_by = by
    interview.save(update_fields=["completed_filing", "next_filing", "updated_by", "updated_at"])


def void_interview(interview: PeriodicInterview, *, by: User | None, reason: str) -> VoidResult:
    """組を取り消す（2 行とも）。タスクに手が付いていなければ自動で戻す。

    戻すのは「次回のタスクが未着手」かつ「完了させたタスクが完了のまま」のときだけ。
    それ以外はタスクを動かさず tasks_reverted = False を返す（画面で手で直すよう伝える）。
    """
    if interview.voided_at is not None:
        raise ValidationError(_("This interview is already voided."), code="voided")
    if not reason.strip():
        raise ValidationError(_("Enter the reason for voiding."), code="void_reason")
    if not workers.is_active(interview.worker):
        raise ValidationError(_("This foreign worker is deactivated."), code="deactivated")
    reason = reason.strip()
    with transaction.atomic():
        interview.voided_at = timezone.now()
        interview.voided_by = by
        interview.void_reason = reason
        interview.updated_by = by
        interview.save(
            update_fields=["voided_at", "voided_by", "void_reason", "updated_by", "updated_at"]
        )
        for record in SupportRecord.objects.filter(interview=interview, voided_at__isnull=True):
            support_records.void_record(record, by=by, reason=reason, with_interview=True)
        next_filing = _fresh(interview.next_filing)
        completed = _fresh(interview.completed_filing)
        untouched_next = next_filing is None or next_filing.status == FilingStatus.NOT_STARTED
        still_completed = completed is None or completed.status == FilingStatus.COMPLETED
        if not (untouched_next and still_completed):
            return VoidResult(tasks_reverted=False)
        note = _("The periodic interview record was voided: %(reason)s") % {"reason": reason}
        if next_filing is not None:
            filings.transition(
                next_filing, FilingStatus.CANCELLED, by=by, reason=note, by_record=True
            )
        if completed is not None:
            filings.transition(
                completed, FilingStatus.NOT_STARTED, by=by, reason=note, by_record=True
            )
    return VoidResult(tasks_reverted=True)


def _fresh(filing: Filing | None) -> Filing | None:
    if filing is None:
        return None
    filing.refresh_from_db()
    return filing


def interviews_of(worker: ForeignWorker) -> QuerySet[PeriodicInterview]:
    return (
        PeriodicInterview.objects.filter(worker=worker)
        .prefetch_related("records__staff", "records__languages")
        .order_by("-basis_on", "-pk")
    )


def warnings(interview: PeriodicInterview) -> list[str]:
    """記録の注意（保存は止めない）。"""
    result = []
    if interview.report_required and interview.reported_on is None:
        result.append(_("A report is required but has not been made yet."))
    return result
