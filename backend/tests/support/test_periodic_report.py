"""定期届出の元データ（T24、ADR-0012）: 別紙1 の支援の欄・特異事案の候補と第4-3号のタスクの照合・
社内の確認用の集計。届出対象期間は 2026-04-01〜2027-03-31（2026 年度）。"""

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.contrib.contenttypes.models import ContentType

from apps.contracts.models import EmploymentContractVersion
from apps.core.models import AuditAction, AuditLog
from apps.filings.models import Filing, FilingKind, FilingStatus, Trigger
from apps.hosts.models import SupportContract
from apps.support.models import ConsultationStatus, SupportRecord
from services import (
    consultations,
    contracts,
    filings,
    periodic_report,
    support_interviews,
    support_plans,
    support_records,
    workers,
)
from services.consultations import EntryInput, ReceptionInput
from services.periodic_report import EventKind, Match, MatchLevel
from services.support_catalog import SupportItem
from services.support_interviews import InterviewInput
from services.support_records import RecordInput
from tests.contracts.factories import make_contract, version_values
from tests.core.factories import make_office, make_organization, make_staff
from tests.hosts.factories import VALID_NUMBERS, make_host, make_site
from tests.support.interview_helpers import answered
from tests.workers.factories import add_residence, make_worker

pytestmark = pytest.mark.django_db

YEAR = 2026
TOKYO = ZoneInfo("Asia/Tokyo")
TODAY = date(2027, 3, 31)


def host_site(name: str = "テスト工業株式会社", number: int = 0) -> Any:
    from services import corporate_number

    base = f"{number + 10:012d}"
    corporate = (
        VALID_NUMBERS[number]
        if number < len(VALID_NUMBERS)
        else f"{corporate_number.check_digit(base)}{base}"
    )
    return make_site(make_host(name=name, corporate_number=corporate))


def full_support(host: Any, starts_on: date = date(2025, 4, 1), **kwargs: Any) -> Any:
    return SupportContract.objects.create(
        host_organization=host, concluded_on=starts_on, starts_on=starts_on, **kwargs
    )


@pytest.fixture
def organization() -> Any:
    return make_organization()


@pytest.fixture
def staff() -> Any:
    return make_staff(name="佐藤", office=make_office(name="本社", phone_number="03-1111-2222"))


@pytest.fixture
def supported(organization: Any, staff: Any) -> Any:
    """2026-04-01〜2027-03-31 の契約、通年で全部委託。"""
    worker = make_worker(assigned_staff=staff)
    site = host_site()
    full_support(site.host_organization)
    make_contract(worker, site=site)
    return worker


def rows_by_worker(year: int = YEAR) -> dict[int, list[Any]]:
    result: dict[int, list[Any]] = {}
    for row in periodic_report.support_rows(year):
        result.setdefault(row.worker.pk, []).append(row)
    return result


# --- 期間・別紙1 の支援の欄 ------------------------------------------------------------------


def test_report_period_is_the_fiscal_year() -> None:
    period = periodic_report.report_period(YEAR)
    assert (period.start, period.end) == (date(2026, 4, 1), date(2027, 3, 31))


def test_row_of_a_worker_supported_all_year(supported: Any, organization: Any) -> None:
    residence = add_residence(supported)
    [row] = rows_by_worker()[supported.pk]
    assert (row.reference_on, row.accepted_from, row.accepted_until) == (
        date(2027, 3, 31),
        date(2026, 4, 1),
        None,  # 3月31日に在籍している
    )
    assert (row.support_from, row.support_until) == (date(2026, 4, 1), date(2027, 3, 31))
    assert row.fully_outsourced_on_reference and row.organization_name == organization.name
    assert (row.staff, row.staff_phone) == (supported.assigned_staff, "03-1111-2222")
    assert row.residence_record_id == residence.pk


def test_ended_contract_uses_the_end_date(staff: Any) -> None:
    worker = make_worker(assigned_staff=staff)
    site = host_site()
    full_support(site.host_organization)
    make_contract(worker, site=site, ends_on=date(2026, 9, 30))
    [row] = rows_by_worker()[worker.pk]
    assert (row.reference_on, row.accepted_until) == (date(2026, 9, 30), date(2026, 9, 30))


def test_transfer_makes_one_row_per_contract_we_supported(staff: Any) -> None:
    worker = make_worker(assigned_staff=staff)
    first = host_site("一社目工業", number=0)
    second = host_site("二社目工業", number=1)
    make_contract(worker, site=first, ends_on=date(2026, 9, 30))
    make_contract(
        worker,
        site=second,
        changed_on=date(2026, 10, 1),
        starts_on=date(2026, 10, 1),
        ends_on=date(2027, 9, 30),
    )
    full_support(second.host_organization)  # 一社目は自機関の支援ではない
    assert [row.host for row in rows_by_worker()[worker.pk]] == [second.host_organization]
    full_support(first.host_organization)
    assert [row.host for row in rows_by_worker()[worker.pk]] == [
        first.host_organization,
        second.host_organization,
    ]


def test_support_contract_period_and_reference_date(staff: Any) -> None:
    worker = make_worker(assigned_staff=staff)
    site = host_site()
    full_support(site.host_organization, starts_on=date(2026, 7, 1), ends_on=date(2027, 1, 31))
    make_contract(worker, site=site)
    [row] = rows_by_worker()[worker.pk]
    assert (row.support_from, row.support_until) == (date(2026, 7, 1), date(2027, 1, 31))
    assert not row.fully_outsourced_on_reference  # 3月31日には自機関の全部委託ではない


def test_site_on_the_reference_date(staff: Any) -> None:
    worker = make_worker(assigned_staff=staff)
    site = host_site()
    full_support(site.host_organization)
    contract = make_contract(worker, site=site)
    moved = make_site(site.host_organization, name="第二工場")
    version = EmploymentContractVersion(
        **version_values(moved, changed_on=date(2026, 12, 1), change_reason="異動")
    )
    contracts.add_version(version, contract=contract, by=None)
    [row] = rows_by_worker()[worker.pk]
    assert row.site == moved


def test_excluded_workers(supported: Any, staff: Any) -> None:
    not_supported = make_worker(assigned_staff=staff)
    make_contract(not_supported, site=host_site("自社支援の会社", number=1))
    outside = make_worker(assigned_staff=staff)
    site = host_site("前年度だけの会社", number=2)
    full_support(site.host_organization)
    make_contract(
        outside,
        site=site,
        changed_on=date(2025, 4, 1),
        starts_on=date(2025, 4, 1),
        ends_on=date(2026, 3, 31),
    )
    found = rows_by_worker()
    assert supported.pk in found and not_supported.pk not in found and outside.pk not in found
    workers.deactivate(supported, by=None, reason="重複")
    assert supported.pk not in rows_by_worker()


# --- 特異事案の候補 --------------------------------------------------------------------------


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
    return support_interviews.create_interview(worker, answered(data), by=None, today=TODAY)


def consultation_with_entry(worker: Any, on: date, **entry: Any) -> Any:
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


def legacy_job_change(worker: Any, on: date, **details: Any) -> SupportRecord:
    """T24a より前の形の転職支援の記録（サービスを通さないので第4-3号のタスクは起票されない）。"""
    return SupportRecord.objects.create(
        worker=worker,
        kind="job_change",
        occurred_on=on,
        method="in_person",
        staff=worker.assigned_staff,
        details={"activities": ["8.a"], **details},
    )


def report_task(worker: Any, basis_on: date) -> Filing:
    filing, _created = filings.create(
        FilingKind.SUPPORT_DIFFICULTY, target=worker, basis_on=basis_on, trigger=Trigger.MANUAL
    )
    return filing


def events_of(
    worker: Any, *kinds: EventKind, **kwargs: Any
) -> list[periodic_report.EventCandidate]:
    return [
        event
        for event in periodic_report.special_events(YEAR, today=TODAY, **kwargs)
        if event.worker == worker and (not kinds or event.kind in kinds)
    ]


REPORTS = (
    EventKind.INTERVIEW_REPORT,
    EventKind.CONSULTATION_REPORT,
    EventKind.HELLO_WORK,
    EventKind.JOB_CHANGE,
)


def test_report_candidates_use_the_report_date_or_the_record_date(supported: Any) -> None:
    with_date = interview(
        supported,
        date(2026, 6, 10),
        violation_suspected=True,
        report_required=True,
        report_to="labor_standards_office",
        reported_on=date(2026, 6, 12),
    )
    without_date = interview(
        supported,
        date(2026, 8, 20),
        violation_suspected=True,
        report_required=True,
        report_to="labor_standards_office",
    )
    interview(supported, date(2026, 11, 1))  # 通報なし
    case = consultation_with_entry(
        supported,
        date(2026, 9, 1),
        reported=True,
        report_to="labor_standards_office",
        hello_work=True,
        hello_work_on=date(2026, 9, 5),
    )
    entry = case.entries.get()
    job = support_records.create_record(
        supported,
        record(
            supported,
            date(2026, 10, 1),
            kind="job_change",
            details={"activities": ["8.b"], "separation": "involuntary"},
        ),
        by=None,
        today=TODAY,
    )
    found = [
        (event.kind, event.occurred_on, event.source_key)
        for event in events_of(supported, *REPORTS)
    ]
    assert found == [
        (EventKind.INTERVIEW_REPORT, date(2026, 6, 12), f"periodic_interview:{with_date.pk}"),
        (EventKind.INTERVIEW_REPORT, date(2026, 8, 20), f"periodic_interview:{without_date.pk}"),
        (EventKind.CONSULTATION_REPORT, date(2026, 9, 1), f"consultation_entry:{entry.pk}:report"),
        (EventKind.HELLO_WORK, date(2026, 9, 5), f"consultation_entry:{entry.pk}:hello_work"),
        (EventKind.JOB_CHANGE, date(2026, 10, 1), f"support_record:{job.pk}"),
    ]
    assert not any(event.needs_review for event in events_of(supported, *REPORTS))
    # 取り消した記録は候補にしない
    support_interviews.void_interview(without_date, by=None, reason="誤り")
    consultations.void_consultation(case, by=None, reason="誤り")
    support_records.void_record(job, by=None, reason="誤り")
    assert [event.source_key for event in events_of(supported, *REPORTS)] == [
        f"periodic_interview:{with_date.pk}"
    ]


def test_matching_with_form_4_3_tasks(supported: Any) -> None:
    job = legacy_job_change(supported, date(2026, 10, 1))

    def match() -> Match:
        [event] = events_of(supported, EventKind.JOB_CHANGE)
        assert event.source_key == f"support_record:{job.pk}"
        result: Match = event.match
        return result

    assert match() == Match(MatchLevel.NONE)
    far = report_task(supported, date(2026, 10, 16))  # 15 日後は近いとみなさない
    assert match() == Match(MatchLevel.NONE)
    near = report_task(supported, date(2026, 9, 17))  # 14 日前
    assert match() == Match(MatchLevel.NEARBY, (near.pk,))
    same = report_task(supported, date(2026, 10, 1))
    assert match() == Match(MatchLevel.SAME_DAY, (same.pk,))
    filings.transition(same, FilingStatus.CANCELLED, by=None, reason="重複")
    assert match() == Match(MatchLevel.NEARBY, (near.pk,))  # 取消のタスクは照合しない
    other = make_worker(assigned_staff=supported.assigned_staff)
    report_task(other, date(2026, 10, 1))  # 別の外国人のタスクは照合しない
    assert match() == Match(MatchLevel.NEARBY, (near.pk,))
    assert far.pk not in match().filing_ids


def test_matcher_can_be_replaced(supported: Any) -> None:
    """T24a で参照による一致に差し替えられる（照合の方法を引数で渡す）。"""
    legacy_job_change(supported, date(2026, 10, 1))
    seen: list[str] = []

    def by_reference(event: Any, filings_: Any) -> Match:
        seen.append(event.source_key)
        return Match(MatchLevel.SAME_DAY, (999,))

    [event] = events_of(supported, EventKind.JOB_CHANGE, matcher=by_reference)
    assert event.match == Match(MatchLevel.SAME_DAY, (999,)) and seen


def test_planned_rows_without_records_need_review(supported: Any) -> None:
    host = supported.employment_contracts.get().host_organization
    support_plans.create_plan(supported, host, applies_from=date(2026, 3, 1), by=None)
    codes = {e.note for e in events_of(supported, EventKind.PLANNED_NOT_RECORDED)}
    assert codes == {
        "1.main", "2.a", "3a.a", "3a.b", "3a.c", "3b.a", "3b.b", "3b.c", "4.main",
        "5.a", "5.b", "5.c", "7.main",
    }  # fmt: skip
    for kind, details, on in (
        (
            "pre_guidance",
            {},
            date(2026, 3, 20),
        ),  # 一度きり: 支援の開始より前でも、契約の開始以降なら数える
        ("airport_transfer", {"direction": "2.a", "airport": "成田"}, date(2026, 4, 2)),
        ("housing_and_contracts", {"activities": ["3a.b", "3b.a"]}, date(2026, 4, 3)),
        ("japanese_learning", {"ways": ["5.b"]}, date(2026, 6, 1)),
        ("exchange", {"event": "夏祭り", "exchange_type": "7.b"}, date(2026, 8, 1)),
    ):
        SupportRecord.objects.create(
            worker=supported,
            kind=kind,
            occurred_on=on,
            method="in_person",
            staff=supported.assigned_staff,
            details=details,
        )
    remaining = {e.note for e in events_of(supported, EventKind.PLANNED_NOT_RECORDED)}
    assert remaining == {"1.main", "3a.a", "3a.c", "3b.b", "3b.c", "4.main", "5.a", "5.c"}
    assert all(e.needs_review for e in events_of(supported, EventKind.PLANNED_NOT_RECORDED))


def test_interview_overdue_needs_review(supported: Any) -> None:
    def overdue() -> list[tuple[date, str]]:
        return [(e.occurred_on, e.note) for e in events_of(supported, EventKind.INTERVIEW_OVERDUE)]

    # 面談が無い: 契約の開始（4/1）から 3 か月の期限は 7/1（ADR-0008）。翌日から超過
    assert overdue() == [(date(2026, 7, 2), "2026-07-01")]
    interview(supported, date(2026, 6, 15))
    interview(supported, date(2026, 9, 15))  # 6/15 から 3 か月の期限（9/15）の当日
    interview(supported, date(2027, 1, 10))  # 9/15 から 3 か月の期限は 12/15。超過
    assert overdue() == [(date(2026, 12, 16), "2026-12-15")]
    # 期間の末日（3/31）・今日より後の超過は出さない（1/10 から 3 か月の期限は 4/10）
    assert all(on <= date(2027, 3, 31) for on, _note in overdue())


# --- 社内の確認・帳簿用の集計 -------------------------------------------------------------------


def test_overview_matches_the_records(supported: Any) -> None:
    interview(supported, date(2026, 6, 15))
    reported = interview(
        supported,
        date(2026, 9, 1),
        violation_suspected=True,
        report_required=True,
        report_to="immigration",
    )
    case = consultation_with_entry(
        supported, date(2026, 10, 5), status_after=ConsultationStatus.RESOLVED
    )
    voided = consultation_with_entry(supported, date(2026, 11, 5))
    consultations.void_consultation(voided, by=None, reason="誤り")
    overview = periodic_report.support_overview(YEAR)
    assert overview.items[supported.pk][SupportItem.PERIODIC_INTERVIEW] == [
        date(2026, 6, 15),
        date(2026, 9, 1),
    ]
    assert overview.items[supported.pk][SupportItem.CONSULTATION] == [date(2026, 10, 5)]
    assert overview.done(supported.pk, SupportItem.PERIODIC_INTERVIEW)
    assert not overview.done(supported.pk, SupportItem.EXCHANGE)
    assert [(c.consultation, c.status, c.reported) for c in overview.consultations] == [
        (case, ConsultationStatus.RESOLVED, False)
    ]
    assert overview.reports == {supported.pk: [f"periodic_interview:{reported.pk}"]}


# --- T24a: 参照による照合・離職の区分・担当者の変更 ------------------------------------


def test_tasks_raised_from_records_match_by_reference(supported: Any) -> None:
    job = support_records.create_record(
        supported,
        record(
            supported,
            date(2026, 10, 1),
            kind="job_change",
            details={"activities": ["8.a"], "separation": "involuntary"},
        ),
        by=None,
        today=TODAY,
    )
    task = Filing.objects.get(kind=FilingKind.SUPPORT_DIFFICULTY, worker=supported)
    [event] = events_of(supported, EventKind.JOB_CHANGE)
    assert event.source_key == f"support_record:{job.pk}"
    assert event.match == Match(MatchLevel.LINKED, (task.pk,))
    # 日付だけで照合する方法も選べる（手で起票したタスクとの照合）
    [by_date] = events_of(supported, EventKind.JOB_CHANGE, matcher=periodic_report.match_by_date)
    assert by_date.match == Match(MatchLevel.SAME_DAY, (task.pk,))


def test_job_change_candidates_follow_the_separation(supported: Any) -> None:
    voluntary = legacy_job_change(supported, date(2026, 10, 1), separation="voluntary")
    unknown = legacy_job_change(supported, date(2026, 10, 2))
    found = [(e.source_key, e.note) for e in events_of(supported, EventKind.JOB_CHANGE)]
    assert found == [(f"support_record:{unknown.pk}", "separation unknown")]
    assert voluntary.pk != unknown.pk


def test_staff_change_after_the_reference_date_needs_review(supported: Any) -> None:
    [row] = rows_by_worker()[supported.pk]
    assert not row.staff_changed_after_reference
    supported.assigned_staff = make_staff(name="鈴木")
    supported.save()  # 監査ログに assigned_staff の変更が残る（今日 2026-10-17。基準日より前）
    assert (
        "assigned_staff"
        in AuditLog.objects.filter(action=AuditAction.UPDATE, object_id=str(supported.pk))
        .latest("pk")
        .changes
    )
    [row] = rows_by_worker()[supported.pk]
    assert not row.staff_changed_after_reference

    def change_logged_at(when: datetime) -> None:
        # 監査ログは追記専用なので、変更の記録を指定の日時で足す
        AuditLog.objects.create(
            action=AuditAction.UPDATE,
            content_type=ContentType.objects.get_for_model(supported),
            object_id=str(supported.pk),
            changes={"assigned_staff": [1, 2]},
            occurred_at=when,
        )

    change_logged_at(datetime(2027, 3, 31, 23, 30, tzinfo=TOKYO))  # 基準日の当日
    [row] = rows_by_worker()[supported.pk]
    assert not row.staff_changed_after_reference
    change_logged_at(datetime(2027, 4, 2, 9, tzinfo=TOKYO))
    [row] = rows_by_worker()[supported.pk]
    assert row.staff_changed_after_reference
