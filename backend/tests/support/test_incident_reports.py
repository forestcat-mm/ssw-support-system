"""第4-3号（特異事案報告）の届出タスクの自動の起票（T24a、ADR-0012）: 起票のきっかけ・期限・
同じ日のまとめ・提出済みのタスク・取消での参照の外しと巻き戻し。"""

from datetime import date
from typing import Any

import pytest
from django.core.exceptions import ValidationError

from apps.core.models import AuditAction, AuditLog
from apps.filings.models import Filing, FilingKind, FilingStatus, Trigger
from apps.support.models import ConsultationStatus
from services import (
    consultations,
    deadlines,
    filings,
    incident_reports,
    support_interviews,
    support_records,
)
from services.consultations import EntryInput, ReceptionInput
from services.support_interviews import InterviewInput
from services.support_records import RecordInput
from tests.contracts.factories import make_contract, site_with_categories
from tests.core.factories import make_staff
from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 17)


@pytest.fixture
def worker() -> Any:
    worker = make_worker(assigned_staff=make_staff(name="佐藤"))
    make_contract(worker, site=site_with_categories())  # 2026-04-01〜2027-03-31
    return worker


def record(worker: Any, on: date, **overrides: Any) -> RecordInput:
    values: dict[str, Any] = {
        "kind": "periodic_interview",
        "occurred_on": on,
        "method": "in_person",
        "staff": worker.assigned_staff,
        "body": "状況を確認した。",
    }
    return RecordInput(**(values | overrides))


def interview(worker: Any, on: date, **overrides: Any) -> Any:
    data = InterviewInput(
        self_row=record(worker, on),
        supervisor_row=record(worker, on, counterpart_name="工場長 田中"),
        **overrides,
    )
    return support_interviews.create_interview(worker, data, by=None, today=TODAY)


def reported_interview(worker: Any, on: date, reported_on: date | None = None) -> Any:
    return interview(
        worker,
        on,
        violation_suspected=True,
        report_required=True,
        report_to="labor_standards_office",
        reported_on=reported_on,
    )


def consultation(worker: Any, on: date, **entry: Any) -> Any:
    case = consultations.create_consultation(
        worker,
        ReceptionInput(
            record=record(worker, on, kind="consultation", method="phone"), category="wages"
        ),
        by=None,
        today=TODAY,
    )
    values: dict[str, Any] = {
        "occurred_on": on,
        "method": "phone",
        "staff": worker.assigned_staff,
        "body": "対応した。",
        "status_after": ConsultationStatus.IN_PROGRESS,
    }
    consultations.add_entry(case, EntryInput(**(values | entry)), by=None, today=TODAY)
    return case


def job_change(worker: Any, on: date, separation: str = "involuntary") -> Any:
    return support_records.create_record(
        worker,
        record(
            worker,
            on,
            kind="job_change",
            details={"activities": ["8.b"], "separation": separation},
        ),
        by=None,
        today=TODAY,
    )


def tasks(worker: Any) -> list[Filing]:
    return list(
        Filing.objects.filter(kind=FilingKind.SUPPORT_DIFFICULTY, worker=worker).order_by("pk")
    )


# --- 起票 ----------------------------------------------------------------------------


def test_interview_report_raises_a_task_with_a_14_day_deadline(worker: Any) -> None:
    case = reported_interview(worker, date(2026, 6, 10), reported_on=date(2026, 6, 12))
    [task] = tasks(worker)
    expected = deadlines.ad_hoc_filing(date(2026, 6, 12))
    assert (task.basis_on, task.due_on, task.status) == (
        date(2026, 6, 12),
        expected.due_on,
        FilingStatus.NOT_STARTED,
    )
    assert task.trigger == Trigger.INCIDENT_RECORDED
    assert task.trigger_details == {"sources": [f"periodic_interview:{case.pk}"]}
    # 通報日が無ければ面談の日
    second = reported_interview(worker, date(2026, 8, 20))
    assert tasks(worker)[1].basis_on == date(2026, 8, 20)
    assert incident_reports.tasks_for([incident_reports.interview_key(second)]) == [
        tasks(worker)[1]
    ]


def test_records_without_an_incident_raise_nothing(worker: Any) -> None:
    interview(worker, date(2026, 6, 10))
    consultation(worker, date(2026, 7, 1))
    job_change(worker, date(2026, 8, 1), separation="voluntary")
    assert tasks(worker) == []


def test_consultation_entry_and_job_change(worker: Any) -> None:
    case = consultation(
        worker,
        date(2026, 9, 1),
        reported=True,
        report_to="labor_standards_office",
        hello_work=True,
    )
    entry = case.entries.get()
    record_ = job_change(worker, date(2026, 10, 1))
    first, second = tasks(worker)
    # 同じ日の通報とハローワーク相談は 1 つのタスクに参照を並べる
    assert first.basis_on == date(2026, 9, 1)
    assert first.trigger_details["sources"] == [
        f"consultation_entry:{entry.pk}:report",
        f"consultation_entry:{entry.pk}:hello_work",
    ]
    assert (second.basis_on, second.trigger_details["sources"]) == (
        date(2026, 10, 1),
        [f"support_record:{record_.pk}"],
    )


def test_same_day_incidents_share_one_task(worker: Any) -> None:
    case = reported_interview(worker, date(2026, 10, 1))
    record_ = job_change(worker, date(2026, 10, 1))
    [task] = tasks(worker)
    assert task.trigger_details["sources"] == [
        f"periodic_interview:{case.pk}",
        f"support_record:{record_.pk}",
    ]
    added = AuditLog.objects.filter(action=AuditAction.UPDATE, object_id=str(task.pk)).latest("pk")
    assert added.context["added_sources"] == [f"support_record:{record_.pk}"]


def test_closed_task_on_the_same_day_gets_a_new_task(worker: Any) -> None:
    """同じ日のタスクが提出済みなら、報告済みの事由と混ぜずに新しいタスクを起票する。"""
    case = reported_interview(worker, date(2026, 10, 1))
    [submitted] = tasks(worker)
    filings.transition(submitted, FilingStatus.SUBMITTED, by=None, submitted_on=date(2026, 10, 5))
    record_ = job_change(worker, date(2026, 10, 1))
    old, new = tasks(worker)
    assert old == submitted and old.trigger_details["sources"] == [f"periodic_interview:{case.pk}"]
    assert (new.basis_on, new.status) == (date(2026, 10, 1), FilingStatus.NOT_STARTED)
    assert new.trigger_details["sources"] == [f"support_record:{record_.pk}"]
    # 同じ日の次の事由は、未完了の新しいタスクにまとまる
    second = reported_interview(worker, date(2026, 10, 1))
    assert len(tasks(worker)) == 2
    new.refresh_from_db()
    assert new.trigger_details["sources"] == [
        f"support_record:{record_.pk}",
        f"periodic_interview:{second.pk}",
    ]
    # 提出済みのタスクを未完了に戻すと同じ日の未完了が 2 件になるので、戻せない
    with pytest.raises(ValidationError) as error:
        filings.transition(submitted, FilingStatus.IN_PROGRESS, by=None)
    assert error.value.code == "unfinished_exists"


# --- 取消での巻き戻し ---------------------------------------------------------------------


def test_voiding_the_only_source_cancels_an_untouched_task(worker: Any) -> None:
    record_ = job_change(worker, date(2026, 10, 1))
    result = support_records.void_record(record_, by=None, reason="別の人の記録")
    [task] = tasks(worker)
    assert task.status == FilingStatus.CANCELLED and "別の人の記録" in task.status_reason
    assert result.cancelled == [task] and not result.needs_attention
    # 取り消した後に同じ日の事由が起きたら、新しく起票できる
    job_change(worker, date(2026, 10, 1))
    assert [t.status for t in tasks(worker)] == [FilingStatus.CANCELLED, FilingStatus.NOT_STARTED]


def test_voiding_one_of_several_sources_keeps_the_task(worker: Any) -> None:
    case = reported_interview(worker, date(2026, 10, 1))
    record_ = job_change(worker, date(2026, 10, 1))
    result = support_records.void_record(record_, by=None, reason="誤り")
    [task] = tasks(worker)
    assert task.status == FilingStatus.NOT_STARTED  # 未着手でも取消にしない
    assert task.trigger_details["sources"] == [f"periodic_interview:{case.pk}"]
    assert result.kept == [task] and not result.cancelled
    removed = AuditLog.objects.filter(action=AuditAction.UPDATE, object_id=str(task.pk)).latest(
        "pk"
    )
    assert removed.context == {
        "removed_sources": [f"support_record:{record_.pk}"],
        "reason": "誤り",
    }
    # 残りの参照も取り消すと、未着手なので取消になる
    outcome = support_interviews.void_interview(case, by=None, reason="誤り")
    assert outcome.incident.cancelled == [task]


def test_voiding_a_touched_task_asks_for_a_manual_fix(worker: Any) -> None:
    case = consultation(worker, date(2026, 9, 1), reported=True, report_to="labor_standards_office")
    [task] = tasks(worker)
    filings.transition(task, FilingStatus.IN_PROGRESS, by=None)
    result = consultations.void_consultation(case, by=None, reason="別の人の相談")
    task.refresh_from_db()
    assert task.status == FilingStatus.IN_PROGRESS and task.trigger_details["sources"] == []
    assert result.needs_attention == [task]
