"""定期届出（参考様式第3-6号・別紙1）の元データ（第5章 F14、T24、ADR-0012）。画面は作らない。

届出対象期間は前年4月1日〜当年3月31日（年度）。登録支援機関は、全部委託を受けた所属機関を経由して連名で届け出る。

1. support_rows: 別紙1 の支援の欄（雇用契約ごと。支援の形態・支援担当者は 3月31日時点）。
2. special_events: 特異事案報告（第4-3号）の候補と、第4-3号の届出タスクとの照合
   （報告済みとは断定しない）。
3. support_overview: 社内の確認・帳簿用の集計（外国人×10項目の実施有無と実施日、相談の一覧、
   通報の有無）。

在留カード番号は復号しない（在留情報の行の ID だけを返す。復号と監査ログは段階4の書き出しで行う）。
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from django.contrib.contenttypes.models import ContentType
from django.db.models import Min
from django.utils import timezone

from apps.contracts.models import EmploymentContract, EmploymentContractVersion
from apps.core.models import AuditAction, AuditLog, Organization, Staff
from apps.filings.models import Filing, FilingKind, FilingStatus
from apps.hosts.models import HostOrganization, HostSite, SupportContract, SupportScope
from apps.support.models import (
    Consultation,
    ConsultationEntry,
    PeriodicInterview,
    SupportPlanActivity,
    SupportRecord,
)
from apps.workers.models import ForeignWorker, LatestResidenceRecord
from services import incident_reports, support_interviews, support_records, workers
from services.support_catalog import SupportItem

MATCH_DAYS = 14  # 照合の幅（候補の日付の前後。第4-3号の報告期限と同じ日数）
REPORT_KIND = FilingKind.SUPPORT_DIFFICULTY  # 第4-3号（登録支援機関の特異事案報告）


@dataclass(frozen=True)
class Period:
    """届出対象期間（年度。両端を含む）。"""

    start: datetime.date
    end: datetime.date

    def overlaps(self, start: datetime.date, end: datetime.date | None) -> bool:
        return start <= self.end and (end is None or end >= self.start)


def report_period(fiscal_year: int) -> Period:
    """届出対象期間（fiscal_year 年4月1日〜翌年3月31日。翌年度の4月1日〜5月31日に提出する）。"""
    return Period(datetime.date(fiscal_year, 4, 1), datetime.date(fiscal_year + 1, 3, 31))


# --- 1. 別紙1 の支援の欄 -------------------------------------------------------------------


@dataclass(frozen=True)
class SupportRow:
    """別紙1 の 1 行（外国人×雇用契約）。事業所ごとの別紙に振り分けるのは書き出しの側。"""

    worker: ForeignWorker
    contract: EmploymentContract
    host: HostOrganization
    site: HostSite  # 基準日の事業所
    reference_on: datetime.date  # 基準日（3月31日、期間中に受入れを終了したらその日）
    accepted_from: datetime.date  # 受入れ開始日（契約の開始日）
    # 期間中（3月31日より前）に受入れを終了した日（備考に書く）
    accepted_until: datetime.date | None
    support_from: datetime.date  # 期間中に自機関が全部委託を受けていた最初の日
    support_until: datetime.date  # 同じく最後の日
    fully_outsourced_on_reference: bool  # 基準日に自機関へ全部委託か（支援の形態）
    organization_name: str
    staff: Staff | None  # 支援担当者（担当の履歴が無いため、作成時点の担当者で代える。ADR-0012）
    staff_phone: str  # 担当者の所属事務所の電話番号
    # 基準日より後に担当者を変えた記録が監査ログにある
    # （3月31日時点の担当者と違うかもしれない。要確認）
    staff_changed_after_reference: bool
    residence_record_id: int | None  # 在留情報の最新行（番号は復号しない）


def _contract_span(contract: EmploymentContract) -> tuple[datetime.date, datetime.date | None]:
    first: datetime.date = getattr(contract, "first_starts_on", None) or contract.current_starts_on
    ends = [d for d in (contract.current_ends_on, contract.terminated_on) if d is not None]
    return first, min(ends) if ends else None


def _version_on(contract: EmploymentContract, on: datetime.date) -> EmploymentContractVersion:
    """基準日に適用されている版（変更日がその日以前の最も新しい版。無ければ第1版）。"""
    versions = sorted(contract.versions.all(), key=lambda version: version.version)
    applied = [version for version in versions if version.changed_on <= on]
    return applied[-1] if applied else versions[0]


def _clip(
    start: datetime.date, end: datetime.date | None, period: Period
) -> tuple[datetime.date, datetime.date]:
    return max(start, period.start), min(end or period.end, period.end)


def _staff_change_dates() -> dict[int, list[datetime.date]]:
    """外国人ごとの、担当者（assigned_staff）を変えた日（監査ログの更新の記録から。東京の日付）。"""
    content_type = ContentType.objects.get_for_model(ForeignWorker)
    logs = AuditLog.objects.filter(
        content_type=content_type,
        action=AuditAction.UPDATE,
        changes__has_key="assigned_staff",
    ).values_list("object_id", "occurred_at")
    result: dict[int, list[datetime.date]] = defaultdict(list)
    for object_id, occurred_at in logs:
        result[int(object_id)].append(timezone.localdate(occurred_at))
    return result


def support_rows(fiscal_year: int) -> list[SupportRow]:
    """期間中に 1 日でも自機関が全部委託を受けていた 1号の外国人の、雇用契約ごとの行。

    雇用契約・全部委託の支援委託契約・届出対象期間の 3 つが重なる日がある契約を対象にする。
    無効化した外国人は含めない。
    """
    period = report_period(fiscal_year)
    organization = Organization.objects.first()
    organization_name = organization.name if organization else ""
    contracts = (
        EmploymentContract.objects.filter(worker__in=workers.active_workers())
        .annotate(first_starts_on=Min("versions__starts_on"))
        .select_related("worker__assigned_staff__office", "host_organization")
        .prefetch_related("versions__site")
        .order_by("worker_id", "current_starts_on", "pk")
    )
    support_contracts: dict[int, list[SupportContract]] = defaultdict(list)
    for support in SupportContract.objects.filter(scope=SupportScope.FULL):
        support_contracts[support.host_organization_id].append(support)
    residence = dict(LatestResidenceRecord.objects.values_list("worker_id", "record_id"))
    staff_changes = _staff_change_dates()

    rows = []
    for contract in contracts:
        starts, ends = _contract_span(contract)
        if not period.overlaps(starts, ends):
            continue
        contract_from, contract_until = _clip(starts, ends, period)
        overlaps = []
        for support in support_contracts.get(contract.host_organization_id, []):
            begin = max(contract_from, support.starts_on)
            finish = min(contract_until, support.effective_ends_on or contract_until)
            if begin <= finish:
                overlaps.append((begin, finish, support))
        if not overlaps:
            continue
        reference_on = contract_until  # 3月31日、期間中に終了したらその日
        staff = contract.worker.assigned_staff
        rows.append(
            SupportRow(
                worker=contract.worker,
                contract=contract,
                host=contract.host_organization,
                site=_version_on(contract, reference_on).site,
                reference_on=reference_on,
                accepted_from=starts,
                # 3月31日に在籍していれば（契約の満了日が 3月31日でも）終了としない
                accepted_until=ends if ends is not None and ends < period.end else None,
                support_from=min(begin for begin, _finish, _support in overlaps),
                support_until=max(finish for _begin, finish, _support in overlaps),
                fully_outsourced_on_reference=any(
                    begin <= reference_on <= finish for begin, finish, _support in overlaps
                ),
                organization_name=organization_name,
                staff=staff,
                staff_phone=staff.office.phone_number if staff is not None else "",
                staff_changed_after_reference=any(
                    changed_on > reference_on
                    for changed_on in staff_changes.get(contract.worker_id, [])
                ),
                residence_record_id=residence.get(contract.worker_id),
            )
        )
    return rows


# --- 2. 特異事案の候補と照合 ----------------------------------------------------------------


class EventKind(StrEnum):
    INTERVIEW_REPORT = "interview_report"  # 定期面談で通報（T19）
    CONSULTATION_REPORT = "consultation_report"  # 相談の経過で通報（T20）
    HELLO_WORK = "hello_work"  # ハローワーク相談（T20）
    JOB_CHANGE = "job_change"  # 非自発的離職時の転職支援（T18）
    PLANNED_NOT_RECORDED = "planned_not_recorded"  # 計画で実施予定「有」の記録なし（要確認）
    INTERVIEW_OVERDUE = "interview_overdue"  # 定期面談の期限の超過（要確認）


NEEDS_REVIEW = frozenset({EventKind.PLANNED_NOT_RECORDED, EventKind.INTERVIEW_OVERDUE})


class MatchLevel(StrEnum):
    LINKED = "linked"  # 一致（記録から起票。タスクが元の記録への参照を持つ。T24a）
    SAME_DAY = "same_day"  # 一致（同日）
    NEARBY = "nearby"  # 近い日付のタスクあり（前後 14 日以内）
    NONE = "none"  # 該当なし


@dataclass(frozen=True)
class Match:
    level: MatchLevel
    filing_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class EventCandidate:
    worker: ForeignWorker
    kind: EventKind
    occurred_on: datetime.date  # 通報日・相談日が入っていればその日、無ければ記録の日
    source_key: str  # 元の記録（"periodic_interview:12" など。T24a の起票の参照と同じ形）
    note: str = ""  # 計画の行の符号・定期面談の法定の期限など
    match: Match = field(default=Match(MatchLevel.NONE))

    @property
    def needs_review(self) -> bool:
        """断定できない候補（計画との突き合わせ・期限の超過）。人が確かめる。"""
        return self.kind in NEEDS_REVIEW


# 照合の方法（special_events の matcher 引数で差し替えられる）。既定は、元の記録への参照
# （T24a の起票の trigger_details["sources"]）を先に見て、無ければ事由発生日の近さで見る。
Matcher = Callable[[EventCandidate, Sequence[Filing]], Match]


def match_by_date(candidate: EventCandidate, filings: Sequence[Filing]) -> Match:
    """同じ外国人の第4-3号のタスク（取消を除く）を、事由発生日の近さで照合する。"""
    same = [f.pk for f in filings if f.basis_on == candidate.occurred_on]
    if same:
        return Match(MatchLevel.SAME_DAY, tuple(same))
    near = [f.pk for f in filings if abs((f.basis_on - candidate.occurred_on).days) <= MATCH_DAYS]
    return Match(MatchLevel.NEARBY, tuple(near)) if near else Match(MatchLevel.NONE)


def match_by_reference(candidate: EventCandidate, filings: Sequence[Filing]) -> Match:
    """元の記録から起票したタスク（参照を持つ）を「一致（記録から起票）」にする。無ければ日付で見る。"""
    linked = [f.pk for f in filings if candidate.source_key in f.trigger_details.get("sources", [])]
    if linked:
        return Match(MatchLevel.LINKED, tuple(linked))
    return match_by_date(candidate, filings)


def _report_events(worker_ids: set[int], period: Period) -> list[EventCandidate]:
    events = []
    interviews = PeriodicInterview.objects.filter(
        worker_id__in=worker_ids, voided_at__isnull=True, report_required=True
    ).select_related("worker")
    for interview in interviews:
        on = interview.reported_on or interview.basis_on
        events.append(
            EventCandidate(
                interview.worker,
                EventKind.INTERVIEW_REPORT,
                on,
                incident_reports.interview_key(interview),
            )
        )
    entries = ConsultationEntry.objects.filter(
        consultation__worker_id__in=worker_ids, consultation__voided_at__isnull=True
    ).select_related("consultation__worker")
    for entry in entries:
        worker = entry.consultation.worker
        if entry.reported:
            on = entry.reported_on or entry.occurred_on
            key = incident_reports.entry_report_key(entry)
            events.append(EventCandidate(worker, EventKind.CONSULTATION_REPORT, on, key))
        if entry.hello_work:
            on = entry.hello_work_on or entry.occurred_on
            key = incident_reports.entry_hello_work_key(entry)
            events.append(EventCandidate(worker, EventKind.HELLO_WORK, on, key))
    job_changes = SupportRecord.objects.filter(
        worker_id__in=worker_ids, kind=SupportItem.JOB_CHANGE, voided_at__isnull=True
    ).select_related("worker")
    for record in job_changes:
        separation = record.details.get("separation", "")
        if separation == "voluntary":
            continue  # 自発的離職の転職支援は特異事案ではない
        events.append(
            EventCandidate(
                record.worker,
                EventKind.JOB_CHANGE,
                record.occurred_on,
                incident_reports.record_key(record),
                note="" if separation else "separation unknown",  # T24a より前の記録
            )
        )
    return [event for event in events if period.start <= event.occurred_on <= period.end]


# 計画の行と記録の突き合わせ（ADR-0007 の符号）。一度きりの支援は支援の開始から基準日まで、
# 継続する支援は期間中に記録があるかを見る。必要に応じて行う支援（出国時の送迎・相談・転職支援）と、
# 定期面談（期限の超過で別に見る）、自由記入の行は見ない。
ONE_TIME_ROWS: dict[str, tuple[SupportItem, str | None]] = {
    "1.main": (SupportItem.PRE_GUIDANCE, None),
    "2.a": (SupportItem.AIRPORT_TRANSFER, "2.a"),
    "3a.a": (SupportItem.HOUSING_AND_CONTRACTS, "3a.a"),
    "3a.b": (SupportItem.HOUSING_AND_CONTRACTS, "3a.b"),
    "3a.c": (SupportItem.HOUSING_AND_CONTRACTS, "3a.c"),
    "3b.a": (SupportItem.HOUSING_AND_CONTRACTS, "3b.a"),
    "3b.b": (SupportItem.HOUSING_AND_CONTRACTS, "3b.b"),
    "3b.c": (SupportItem.HOUSING_AND_CONTRACTS, "3b.c"),
    "4.main": (SupportItem.ORIENTATION, None),
}
RECURRING_ROWS: dict[str, tuple[SupportItem, str | None]] = {
    "5.a": (SupportItem.JAPANESE_LEARNING, "5.a"),
    "5.b": (SupportItem.JAPANESE_LEARNING, "5.b"),
    "5.c": (SupportItem.JAPANESE_LEARNING, "5.c"),
    "7.main": (SupportItem.EXCHANGE, None),
}


def _record_codes(record: SupportRecord) -> set[str]:
    codes: set[str] = set()
    for value in record.details.values():
        if isinstance(value, list):
            codes.update(str(v) for v in value)
        elif isinstance(value, str):
            codes.add(value)
    return codes


def _has_record(
    records: Iterable[SupportRecord],
    item: SupportItem,
    code: str | None,
    start: datetime.date,
    end: datetime.date,
) -> bool:
    return any(
        record.kind == item
        and start <= record.occurred_on <= end
        and (code is None or code in _record_codes(record))
        for record in records
    )


def _plan_events(rows: list[SupportRow], period: Period) -> list[EventCandidate]:
    records_by_worker: dict[int, list[SupportRecord]] = defaultdict(list)
    for record in SupportRecord.objects.filter(
        worker_id__in={row.worker.pk for row in rows}, voided_at__isnull=True
    ):
        records_by_worker[record.worker_id].append(record)
    events = []
    for row in rows:
        version = support_records.plan_version_for(row.worker, row.support_until)
        if version is None:
            continue
        records = records_by_worker[row.worker.pk]
        planned = SupportPlanActivity.objects.filter(version=version, planned=True)
        for activity in planned:
            if activity.code in ONE_TIME_ROWS:
                item, code = ONE_TIME_ROWS[activity.code]
                found = _has_record(records, item, code, row.accepted_from, row.support_until)
            elif activity.code in RECURRING_ROWS:
                item, code = RECURRING_ROWS[activity.code]
                found = _has_record(records, item, code, row.support_from, row.support_until)
            else:
                continue
            if not found:
                events.append(
                    EventCandidate(
                        row.worker,
                        EventKind.PLANNED_NOT_RECORDED,
                        row.support_until,
                        f"plan_activity:{activity.pk}",
                        note=activity.code,
                    )
                )
    return events


def _overdue_events(
    rows: list[SupportRow], period: Period, today: datetime.date
) -> list[EventCandidate]:
    """定期面談の法定の期限（前回から 3 か月）を超えた日（期限の翌日）。期間中・今日までに限る。"""
    events = []
    limit = min(period.end, today)
    for row in rows:
        dates = sorted(
            PeriodicInterview.objects.filter(worker=row.worker, voided_at__isnull=True).values_list(
                "basis_on", flat=True
            )
        )
        before = [d for d in dates if d <= row.support_from]
        basis = before[-1] if before else row.accepted_from
        end = min(limit, row.support_until)
        while True:
            due = support_interviews.legal_due_on(basis)
            following = next((d for d in dates if d > basis), None)
            overdue_on = due + datetime.timedelta(days=1)
            if overdue_on > end:
                break
            if following is None or following > due:
                if overdue_on >= period.start:
                    events.append(
                        EventCandidate(
                            row.worker,
                            EventKind.INTERVIEW_OVERDUE,
                            overdue_on,
                            f"interview_overdue:{row.worker.pk}:{due.isoformat()}",
                            note=due.isoformat(),
                        )
                    )
                if following is None:
                    break
            basis = following
    return events


def special_events(
    fiscal_year: int, *, today: datetime.date | None = None, matcher: Matcher = match_by_reference
) -> list[EventCandidate]:
    """特異事案報告（第4-3号）の候補と、第4-3号の届出タスクとの照合の結果（日付・種類の順）。

    照合は「一致（記録から起票）」「一致（同日）」「近い日付のタスクあり」「該当なし」と
    タスクの ID まで。報告済みとは断定しない。
    """
    today = today or timezone.localdate()
    period = report_period(fiscal_year)
    rows = support_rows(fiscal_year)
    worker_ids = {row.worker.pk for row in rows}
    candidates = (
        _report_events(worker_ids, period)
        + _plan_events(rows, period)
        + _overdue_events(rows, period, today)
    )
    filings_by_worker: dict[int, list[Filing]] = defaultdict(list)
    for filing in Filing.objects.filter(kind=REPORT_KIND, worker_id__in=worker_ids).exclude(
        status=FilingStatus.CANCELLED
    ):
        if filing.worker_id is not None:
            filings_by_worker[filing.worker_id].append(filing)
    return sorted(
        (
            EventCandidate(
                event.worker,
                event.kind,
                event.occurred_on,
                event.source_key,
                event.note,
                matcher(event, filings_by_worker[event.worker.pk]),
            )
            for event in candidates
        ),
        key=lambda event: (event.worker.pk, event.occurred_on, event.kind, event.source_key),
    )


# --- 3. 社内の確認・帳簿用の集計 ---------------------------------------------------------------


@dataclass(frozen=True)
class ConsultationSummary:
    consultation: Consultation
    received_on: datetime.date
    category: str
    status: str
    reported: bool
    hello_work: bool


@dataclass(frozen=True)
class Overview:
    # 外国人 ID → 種別 → 実施日（昇順。取り消した記録は含めない）
    items: dict[int, dict[SupportItem, list[datetime.date]]]
    consultations: list[ConsultationSummary]
    # 外国人 ID → 通報の元の記録（"periodic_interview:12"・"consultation_entry:3"）
    reports: dict[int, list[str]]

    def done(self, worker_id: int, item: SupportItem) -> bool:
        return bool(self.items.get(worker_id, {}).get(item))


def support_overview(fiscal_year: int) -> Overview:
    """期間中の、外国人×10項目の実施日・相談の一覧・通報の有無（定期届出の欄ではない。ADR-0012）。"""
    period = report_period(fiscal_year)
    worker_ids = {row.worker.pk for row in support_rows(fiscal_year)}
    items: dict[int, dict[SupportItem, list[datetime.date]]] = {}
    records = SupportRecord.objects.filter(
        worker_id__in=worker_ids,
        voided_at__isnull=True,
        declined_by_worker=False,  # 本人の申出により実施しなかった記録は実施日にしない（T44）
        occurred_on__gte=period.start,
        occurred_on__lte=period.end,
    ).order_by("occurred_on", "pk")
    for record in records:
        dates = items.setdefault(record.worker_id, {}).setdefault(SupportItem(record.kind), [])
        if record.occurred_on not in dates:
            dates.append(record.occurred_on)
    consultations = [
        ConsultationSummary(
            consultation=consultation,
            received_on=consultation.record.occurred_on,
            category=consultation.category,
            status=consultation.status,
            reported=consultation.reported,
            hello_work=consultation.hello_work,
        )
        for consultation in Consultation.objects.filter(
            worker_id__in=worker_ids,
            voided_at__isnull=True,
            record__occurred_on__gte=period.start,
            record__occurred_on__lte=period.end,
        )
        .select_related("record")
        .order_by("record__occurred_on", "pk")
    ]
    reports: dict[int, list[str]] = {}
    for event in _report_events(worker_ids, period):
        if event.kind in (EventKind.INTERVIEW_REPORT, EventKind.CONSULTATION_REPORT):
            reports.setdefault(event.worker.pk, []).append(event.source_key)
    return Overview(items=items, consultations=consultations, reports=reports)
