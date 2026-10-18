"""定期面談（T19）: 期限・完了の遷移・記録と次回のタスク・取消・日次の起票・画面。"""

from datetime import date
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.test import Client

from apps.filings.models import Filing, FilingKind, FilingStatus, Trigger
from apps.support.models import PeriodicInterview, ReportDestination, SupportRecord
from apps.workers.models import WorkerStatus
from services import deadlines, filings, support_interviews, support_records, workers
from services.support_interviews import InterviewInput
from services.support_records import RecordInput
from tests.contracts.factories import make_contract, site_with_categories
from tests.core.factories import make_staff
from tests.hosts.factories import VALID_NUMBERS, make_host, make_site
from tests.support.interview_helpers import answer_fields, answered
from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 17)


@pytest.fixture
def worker() -> Any:
    worker = make_worker(assigned_staff=make_staff(name="佐藤"), status=WorkerStatus.RESIDING)
    make_contract(worker, site=site_with_categories())  # 2026-04-01〜2027-03-31
    return worker


def row(worker: Any, on: date = date(2026, 7, 1), **overrides: Any) -> RecordInput:
    values: dict[str, Any] = {
        "kind": "periodic_interview",
        "occurred_on": on,
        "method": "in_person",
        "staff": worker.assigned_staff,
        "body": "生活・仕事の状況を確認した。",
    }
    return RecordInput(**(values | overrides))


def interview(worker: Any, on: date = date(2026, 7, 1), **overrides: Any) -> InterviewInput:
    values: dict[str, Any] = {
        "self_row": row(worker, on),
        "supervisor_row": row(worker, on, counterpart_name="工場長 田中"),
    }
    return answered(InterviewInput(**(values | overrides)))


def open_task(worker: Any, basis_on: date = date(2026, 4, 1)) -> Filing:
    filing, _created = filings.create(
        FilingKind.PERIODIC_INTERVIEW,
        target=worker,
        basis_on=basis_on,
        trigger=Trigger.INTERVIEW_CYCLE,
    )
    return filing


def keys(error: pytest.ExceptionInfo[ValidationError]) -> set[str]:
    return set(error.value.error_dict)


# --- 期限 -------------------------------------------------------------------------------


def test_scheduled_date_is_the_due_date_and_legal_limit_is_kept() -> None:
    plain = deadlines.periodic_interview(date(2026, 7, 1))
    assert (plain.due_on, plain.final_on) == (date(2026, 10, 1), None)  # 民法第143条
    scheduled = deadlines.periodic_interview(date(2026, 7, 1), date(2026, 9, 15))
    assert (scheduled.due_on, scheduled.unadjusted_due_on) == (date(2026, 9, 15),) * 2
    assert scheduled.final_on == date(2026, 10, 1)
    assert deadlines.periodic_interview(date(2026, 7, 1), date(2026, 10, 1)).due_on == date(
        2026, 10, 1
    )
    with pytest.raises(ValueError):
        deadlines.periodic_interview(date(2026, 7, 1), date(2026, 10, 2))
    with pytest.raises(ValueError):
        deadlines.periodic_interview(date(2026, 7, 1), date(2026, 7, 1))


# --- 状態「完了」 -------------------------------------------------------------------------


def test_completed_is_only_for_internal_tasks(worker: Any) -> None:
    contract = worker.employment_contracts.get()
    immigration, _ = filings.create(
        FilingKind.CONTRACT_CHANGE, target=contract, basis_on=TODAY, trigger=Trigger.MANUAL
    )
    assert FilingStatus.COMPLETED not in filings.allowed_transitions(immigration)
    with pytest.raises(ValidationError):
        filings.transition(immigration, FilingStatus.COMPLETED, by=None)

    passport_check, _ = filings.create(
        FilingKind.PASSPORT_CHECK,
        target=contract,
        basis_on=date(2027, 1, 1),
        trigger=Trigger.MANUAL,
    )
    filings.transition(passport_check, FilingStatus.COMPLETED, by=None)
    with pytest.raises(ValidationError):  # 完了の取り消しは理由が必須
        filings.transition(passport_check, FilingStatus.IN_PROGRESS, by=None)
    filings.transition(passport_check, FilingStatus.IN_PROGRESS, by=None, reason="確認漏れ")
    assert passport_check.status == FilingStatus.IN_PROGRESS


def test_periodic_interview_task_is_completed_only_by_recording(worker: Any) -> None:
    task = open_task(worker)
    assert FilingStatus.COMPLETED not in filings.allowed_transitions(task)
    with pytest.raises(ValidationError):
        filings.transition(task, FilingStatus.COMPLETED, by=None)
    filings.transition(task, FilingStatus.COMPLETED, by=None, by_record=True)
    assert filings.allowed_transitions(task) == frozenset()  # 手では戻せない


# --- 記録 -------------------------------------------------------------------------------


def test_record_completes_the_task_and_creates_the_next(worker: Any) -> None:
    task = open_task(worker)
    data = interview(worker, self_row=row(worker, date(2026, 7, 2)))  # 上司は 7/1、本人は 7/2
    result = support_interviews.create_interview(worker, data, by=None, task=task, today=TODAY)
    task.refresh_from_db()
    assert task.status == FilingStatus.COMPLETED
    assert result.completed_filing == task
    assert result.basis_on == date(2026, 7, 1)  # 2 行の早い方
    following = result.next_filing
    assert following is not None
    assert (following.basis_on, following.due_on) == (date(2026, 7, 1), date(2026, 10, 1))
    assert following.status == FilingStatus.NOT_STARTED
    rows = SupportRecord.objects.filter(interview=result).order_by("counterpart")
    assert [(r.counterpart, r.counterpart_name) for r in rows] == [
        ("supervisor", "工場長 田中"),
        ("worker", ""),
    ]


def test_scheduled_date_becomes_the_next_due_date(worker: Any) -> None:
    open_task(worker)
    data = interview(worker, next_scheduled_on=date(2026, 9, 10))
    result = support_interviews.create_interview(worker, data, by=None, today=TODAY)
    following = result.next_filing
    assert following is not None
    assert (following.due_on, following.final_on) == (date(2026, 9, 10), date(2026, 10, 1))
    # 起算日を直しても、予定日が新しい法定の期限の範囲内なら保つ
    filings.rebase(following, date(2026, 6, 30), by=None, reason="面談日の誤り")
    assert (following.due_on, following.final_on) == (date(2026, 9, 10), date(2026, 9, 30))


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"next_scheduled_on": date(2026, 10, 2)}, {"next_scheduled_on"}),
        ({"next_scheduled_on": date(2026, 7, 1)}, {"next_scheduled_on"}),
        ({"violation_suspected": True}, {"report_required"}),
        ({"violation_suspected": True, "report_required": True}, {"report_to"}),
        (
            {"violation_suspected": True, "report_required": True, "report_to": "other"},
            {"report_to_note"},
        ),
        ({"report_required": True}, {"report_required"}),
        # 違反の疑いが「無」なら、通報の欄は 1 つでも入っていれば弾く（保存時に捨てない）
        ({"report_to": "labor_standards_office"}, {"report_required"}),
        ({"report_to_note": "市役所"}, {"report_required"}),
        ({"reported_on": date(2026, 7, 2)}, {"report_required"}),
        # 要否が「不要」なら、通報先・通報先（詳細）・通報日は弾く
        (
            {
                "violation_suspected": True,
                "report_required": False,
                "report_to": "labor_standards_office",
            },
            {"report_to"},
        ),
        (
            {"violation_suspected": True, "report_required": False, "report_to_note": "市役所"},
            {"report_to_note"},
        ),
        (
            {
                "violation_suspected": True,
                "report_required": False,
                "reported_on": date(2026, 7, 2),
            },
            {"reported_on"},
        ),
    ],
)
def test_validate_findings(worker: Any, overrides: dict[str, Any], expected: set[str]) -> None:
    with pytest.raises(ValidationError) as error:
        support_interviews.validate(worker, interview(worker, **overrides), today=TODAY)
    assert keys(error) == expected


def test_rows_reject_phone_and_need_the_supervisor_name(worker: Any) -> None:
    data = interview(
        worker,
        self_row=row(worker, method="phone"),
        supervisor_row=row(worker, counterpart_name=" "),
    )
    with pytest.raises(ValidationError) as error:
        support_interviews.validate(worker, data, today=TODAY)
    assert keys(error) == {"self.method", "supervisor.counterpart_name"}


def test_counterparts_are_fixed_per_row(worker: Any) -> None:
    data = interview(
        worker,
        self_row=row(worker, counterpart="supervisor"),  # 本人用の行は本人に固定される
    )
    rows = support_interviews.validate(worker, data, today=TODAY)
    assert (rows["self"].counterpart, rows["supervisor"].counterpart) == ("worker", "supervisor")


def test_report_fields_are_saved(worker: Any) -> None:
    data = interview(
        worker,
        violation_suspected=True,
        report_required=True,
        report_to=ReportDestination.LABOR_STANDARDS_OFFICE,
    )
    result = support_interviews.create_interview(worker, data, by=None, today=TODAY)
    assert (result.report_to, result.reported_on) == ("labor_standards_office", None)
    assert support_interviews.warnings(result) == [
        "通報が「要」ですが、まだ通報日が入っていません。"
    ]


def test_backfilled_older_interview_does_not_move_tasks(worker: Any) -> None:
    open_task(worker)
    newer = support_interviews.create_interview(
        worker, interview(worker, date(2026, 7, 1)), by=None, today=TODAY
    )
    older = support_interviews.create_interview(
        worker, interview(worker, date(2026, 5, 1)), by=None, today=TODAY
    )
    assert (older.completed_filing, older.next_filing) == (None, None)
    assert newer.next_filing is not None
    newer.next_filing.refresh_from_db()
    assert newer.next_filing.status == FilingStatus.NOT_STARTED


# --- 取消 -------------------------------------------------------------------------------


def test_void_reverts_untouched_tasks(worker: Any) -> None:
    task = open_task(worker)
    result = support_interviews.create_interview(worker, interview(worker), by=None, today=TODAY)
    first = result.records.first()
    assert first is not None
    with pytest.raises(ValidationError):  # 行ごとには取り消せない
        support_records.void_record(first, by=None, reason="誤り")
    outcome = support_interviews.void_interview(result, by=None, reason="別の人の面談")
    assert outcome.tasks_reverted
    task.refresh_from_db()
    assert task.status == FilingStatus.NOT_STARTED
    following = Filing.objects.get(pk=result.next_filing_id or 0)
    assert following.status == FilingStatus.CANCELLED
    assert "別の人の面談" in following.status_reason
    assert not SupportRecord.objects.filter(interview=result, voided_at__isnull=True).exists()


def test_void_leaves_touched_tasks(worker: Any) -> None:
    task = open_task(worker)
    result = support_interviews.create_interview(worker, interview(worker), by=None, today=TODAY)
    following = Filing.objects.get(pk=result.next_filing_id or 0)
    filings.transition(following, FilingStatus.IN_PROGRESS, by=None)
    outcome = support_interviews.void_interview(result, by=None, reason="別の人の面談")
    assert not outcome.tasks_reverted
    task.refresh_from_db()
    following.refresh_from_db()
    assert (task.status, following.status) == (FilingStatus.COMPLETED, FilingStatus.IN_PROGRESS)


# --- 日次の起票 ----------------------------------------------------------------------------


def test_daily_creates_from_contract_start_then_from_last_interview(worker: Any) -> None:
    pre_entry = make_worker()  # 入国前・契約なし → 対象外
    result = filings.create_scheduled(TODAY)
    created = [f for f in result.created if f.kind == FilingKind.PERIODIC_INTERVIEW]
    assert [(f.worker_id, f.basis_on, f.due_on) for f in created] == [
        (worker.pk, date(2026, 4, 1), date(2026, 7, 1))
    ]
    assert not Filing.objects.filter(worker=pre_entry).exists()
    # 開いているタスクがあれば作らない（冪等）
    again = filings.create_scheduled(TODAY)
    assert not [f for f in again.created if f.kind == FilingKind.PERIODIC_INTERVIEW]
    # 記録 → 次回のタスクは記録のときに作られている。取り消しても日次で重ねない
    support_interviews.create_interview(worker, interview(worker), by=None, today=TODAY)
    assert Filing.objects.filter(
        worker=worker, kind=FilingKind.PERIODIC_INTERVIEW, status=FilingStatus.NOT_STARTED
    ).values_list("basis_on", flat=True).get() == date(2026, 7, 1)


def test_daily_keeps_the_last_interview_across_a_job_change() -> None:
    """転籍しても「3か月に1回以上」は途切れない。起算日は前の受入れ機関での面談日のまま。"""
    worker = make_worker(assigned_staff=make_staff(name="佐藤"), status=WorkerStatus.RESIDING)
    make_contract(
        worker,
        site=make_site(make_host(name="旧工業株式会社", corporate_number=VALID_NUMBERS[0])),
        changed_on=date(2025, 4, 1),
        starts_on=date(2025, 4, 1),
        ends_on=date(2026, 3, 31),
    )
    make_contract(
        worker,
        site=make_site(make_host(name="新工業株式会社", corporate_number=VALID_NUMBERS[1])),
        changed_on=date(2026, 4, 1),
        starts_on=date(2026, 4, 1),
        ends_on=date(2027, 3, 31),
    )
    saved = support_interviews.create_interview(
        worker, interview(worker, date(2026, 3, 10)), by=None, today=TODAY
    )
    assert saved.next_filing is not None
    filings.transition(saved.next_filing, FilingStatus.CANCELLED, by=None, reason="確認のため")
    result = filings.create_scheduled(TODAY)
    created = [f for f in result.created if f.kind == FilingKind.PERIODIC_INTERVIEW]
    # 今の契約の開始日（4/1）ではなく、前回の面談日（3/10）から 3 か月
    assert [(f.basis_on, f.due_on) for f in created] == [(date(2026, 3, 10), date(2026, 6, 10))]


def test_daily_skips_deactivated_workers(worker: Any) -> None:
    workers.deactivate(worker, by=None, reason="重複")
    result = filings.create_scheduled(TODAY)
    assert not [f for f in result.created if f.kind == FilingKind.PERIODIC_INTERVIEW]


# --- 画面 -------------------------------------------------------------------------------


def post_data(page: Any, **overrides: Any) -> dict[str, Any]:
    editor = page.context["editor"]
    data: dict[str, Any] = {"once_token": page.context["once_token"]}
    for form in editor.forms():
        for bound in form:
            value = bound.value()
            if value is None or bound.name.endswith("uploads"):
                continue
            if isinstance(value, (list, tuple)):
                data[bound.html_name] = [str(v) for v in value]
            else:
                data[bound.html_name] = str(getattr(value, "pk", value))
    data.update(
        {
            "self-occurred_on": "2026-10-01",
            "self-body": "仕事に慣れてきた。",
            "supervisor-occurred_on": "2026-10-01",
            "supervisor-counterpart_name": "工場長 田中",
            "supervisor-body": "勤務態度は良好。",
        }
        | answer_fields(date(2026, 10, 1))
    )
    data.update(overrides)
    return data


def test_dashboard_link_records_and_completes(support_staff_client: Client, worker: Any) -> None:
    task = open_task(worker)
    dashboard = support_staff_client.get("/").content.decode()
    url = f"/workers/{worker.pk}/support/interviews/new/?filing={task.pk}"
    assert url.replace("&", "&amp;") in dashboard or url in dashboard
    detail = support_staff_client.get(f"/filings/{task.pk}/").content.decode()
    assert 'id="record-interview"' in detail
    page = support_staff_client.get(url)
    html = page.content.decode()
    assert 'name="self-method"' in html and 'value="phone"' not in html
    assert 'name="self-counterpart"' not in html and 'name="supervisor-counterpart_name"' in html
    assert "data-keepalive=" in html and 'name="filing"' in html
    response = support_staff_client.post(url, post_data(page, filing=task.pk))
    saved = PeriodicInterview.objects.get(worker=worker)
    assert response["Location"] == f"/workers/{worker.pk}/support/interviews/{saved.pk}/"
    task.refresh_from_db()
    assert task.status == FilingStatus.COMPLETED
    detail = support_staff_client.get(f"/filings/{task.pk}/").content.decode()
    assert f"/support/interviews/{saved.pk}/" in detail
    shown = support_staff_client.get(response["Location"]).content.decode()
    assert "工場長 田中" in shown and "定期面談（2026-10-01）" in shown


def test_errors_are_shown_under_the_row_fields(support_staff_client: Client, worker: Any) -> None:
    url = f"/workers/{worker.pk}/support/interviews/new/"
    page = support_staff_client.get(url)
    response = support_staff_client.post(
        url, post_data(page, **{"supervisor-counterpart_name": ""})
    )
    assert response.status_code == 200
    assert 'id="id_supervisor-counterpart_name-error"' in response.content.decode()
    page = support_staff_client.get(url)
    response = support_staff_client.post(  # サービスの検査（法定の 3 か月を超える予定日）
        url, post_data(page, **{"interview-next_scheduled_on": "2027-03-01"})
    )
    assert 'id="id_interview-next_scheduled_on-error"' in response.content.decode()
    assert not PeriodicInterview.objects.exists()


def test_report_fields_follow_the_violation_and_the_need(
    support_staff_client: Client, worker: Any
) -> None:
    """通報の欄は違反の疑い・要否で有効・無効になる（forms.js）。JS が無くても保存時に弾く。"""
    url = f"/workers/{worker.pk}/support/interviews/new/"
    page = support_staff_client.get(url)
    html = page.content.decode()
    assert html.count('data-enabled-when="interview-violation_suspected=yes"') == 2  # 要・不要
    rule = 'data-enabled-when="interview-violation_suspected=yes interview-report_required=yes"'
    assert html.count(rule) == 3  # 通報先・通報先（詳細）・通報日
    response = support_staff_client.post(
        url,
        post_data(
            page,
            **{"interview-violation_suspected": "no", "interview-report_to_note": "市役所"},
        ),
    )
    assert 'id="id_interview-report_required-error"' in response.content.decode()
    assert not PeriodicInterview.objects.exists()


def test_void_screen_and_list(support_staff_client: Client, worker: Any) -> None:
    saved = support_interviews.create_interview(worker, interview(worker), by=None, today=TODAY)
    records = support_staff_client.get(f"/workers/{worker.pk}/support/records/").content.decode()
    assert f"/support/interviews/{saved.pk}/" in records
    record = saved.records.first()
    assert record is not None
    void_one = f"/workers/{worker.pk}/support/records/{record.pk}/void/"
    assert support_staff_client.get(void_one).status_code == 404
    url = f"/workers/{worker.pk}/support/interviews/{saved.pk}/void/"
    page = support_staff_client.get(url)
    response = support_staff_client.post(
        url, {"once_token": page.context["once_token"], "reason": "別の人の面談"}
    )
    assert response.status_code == 302
    saved.refresh_from_db()
    assert saved.voided_at is not None
    html = support_staff_client.get(response["Location"]).content.decode()
    assert 'id="interview-voided"' in html


def test_manual_transition_form_has_no_completed_for_interviews(
    support_staff_client: Client, worker: Any
) -> None:
    task = open_task(worker)
    page = support_staff_client.get(f"/filings/{task.pk}/")
    choices = [value for value, _label in page.context["transition_form"].fields["to"].choices]
    assert "completed" not in choices and "in_progress" in choices
