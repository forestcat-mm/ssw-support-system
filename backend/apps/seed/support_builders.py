"""seed_dummy の段階2（T26）: 支援計画・定期面談・相談・事前ガイダンスと生活オリエンテーション・
転職支援・交付記録・要配慮添付。docs/dummy-data.md の「段階2の場面」の表の番号で作る
（外国人の番号は 1 から）。

- 業務のルール（検査・タスクの起票・照合）は services をそのまま呼ぶ（today は基準日）。
- 場面の外国人の番号は、事業所（番号順に 精機本社・精機第二・食品工場）と担当者
  （佐藤・鈴木を交互）で確定した。
- 意図した警告以外が出ないように作る（tests/seed/test_seed_support.py が警告の一覧を固定する）。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, time, timedelta
from typing import Any

from django.contrib.contenttypes.models import ContentType
from django.core.files.base import ContentFile
from django.db import models

from apps.contracts.models import EmploymentContract
from apps.core.models import Attachment
from apps.filings.models import Filing, FilingKind, FilingStatus
from apps.seed.context import DUMMY_MARK, DUMMY_PDF, SeedContext
from apps.support.models import (
    Briefing,
    BriefingCheck,
    BriefingForeignFee,
    BriefingSession,
    Consultation,
    ConsultationEntry,
    ConsultationStatus,
    DocumentDelivery,
    PeriodicInterview,
    SupportPlan,
    SupportPlanActivity,
    SupportPlanConsultation,
    SupportPlanHousing,
    SupportPlanItem,
    SupportPlanMunicipalityCheck,
    SupportPlanVersion,
    SupportRecord,
)
from apps.workers.models import ForeignWorker
from services import (
    briefings,
    consultations,
    deliveries,
    filings,
    support_interviews,
    support_plans,
    support_records,
    worker_documents,
)
from services.briefings import BriefingInput, CheckInput, FeeInput, SessionInput
from services.consultations import EntryInput, ReceptionInput
from services.deliveries import DeliveryInput
from services.support_catalog import SupportItem
from services.support_interviews import InterviewInput
from services.support_records import RecordInput

# --- 場面の外国人の番号（docs/dummy-data.md の表と同じ） ---------------------------------------

FOOD_NUMBERS = (3, 6, 9, 12, 15, 18, 21, 24, 27, 30)  # 食品工場（番号 3 の倍数）
NOT_RESIDING = frozenset({4, 6, 25})  # 自己都合で離職・会社都合で離職・入国前（定期面談なし）
HEALTH_DOCUMENTS = (1, 2)  # 佐藤の担当・鈴木の担当
REPORTED_INTERVIEW = (
    7  # 通報「要」の組・通報とハローワーク相談の経過・第4-3号の閉じたタスクと新しいタスク
)
VOIDED_INTERVIEW = 8
HANDED_OVER = 11
REOPENED = 13
INVOLUNTARY_JOB_CHANGE = 6
VOLUNTARY_JOB_CHANGE = 4
GUIDANCE_COMPLETE = 25  # 入国前。費用の確認つき・署名済み
GUIDANCE_SHORT = 10  # 目安の 3 時間を下回る・署名なし
ORIENTATION_NOT_APPLICABLE = 12  # 該当なしの細目・署名済み
ORIENTATION_SHORT = 14  # 必要な 8 時間以上を満たさない・署名なし
CAREER_PLAN_MISSING = 15  # 食品の契約でキャリアアッププランが未交付
TRANSFER = 9  # 転籍。前の契約は交付済み、新しい契約が未交付（builders.seed_transfer）

# 定期面談の次回の期限の分布（最後の面談の日を基準日から逆算する。3 か月は 89〜92 日）
OVERDUE = (1, 10, 16)  # 最後の面談 D−100 → 期限は D−11〜D−8
WITHIN_7 = (2, 11, 17, 22)  # D−88 → D+1〜D+4
WITHIN_14 = (3, 12, 18, 23)  # D−80 → D+9〜D+12
REPORT_DAY = -5  # 7 番の通報・6 番の転職支援の日（第4-3号の期限が 14 日以内に入る）


def _pdf(name: str) -> ContentFile[bytes]:
    return ContentFile(DUMMY_PDF, name=name)


def _contract(worker: ForeignWorker) -> EmploymentContract:
    """最初の雇用契約（転籍の人は前の契約）。"""
    return EmploymentContract.objects.filter(worker=worker).order_by("current_starts_on").first()  # type: ignore[return-value]


def _starts_on(contract: EmploymentContract) -> date:
    return contract.versions.order_by("version").first().starts_on  # type: ignore[union-attr]


def _signed_on(contract: EmploymentContract) -> date:
    return contract.versions.order_by("version").first().signed_on  # type: ignore[union-attr,return-value]


# --- 支援計画 --------------------------------------------------------------------------------


def seed_plans(ctx: SeedContext) -> None:
    for worker in ctx.workers:
        for contract in EmploymentContract.objects.filter(worker=worker).order_by(
            "current_starts_on"
        ):
            support_plans.create_plan(
                worker,
                contract.host_organization,
                applies_from=_starts_on(contract) - timedelta(days=30),
                by=None,
            )


# --- 定期面談 --------------------------------------------------------------------------------


def _row(ctx: SeedContext, worker: ForeignWorker, on: date, **overrides: Any) -> RecordInput:
    values: dict[str, Any] = {
        "kind": SupportItem.PERIODIC_INTERVIEW,
        "occurred_on": on,
        "start_time": time(10, 0),
        "duration_minutes": 30,
        "method": "in_person",
        "staff": worker.assigned_staff,
        "languages": [worker.support_language],
        "body": f"仕事と生活の状況を確認した。特に問題はない。{DUMMY_MARK}",
    }
    return RecordInput(**(values | overrides))


def _interview(ctx: SeedContext, worker: ForeignWorker, on: date, **overrides: Any) -> Any:
    data = InterviewInput(
        self_row=_row(ctx, worker, on),
        supervisor_row=_row(
            ctx,
            worker,
            on,
            counterpart_name=f"工場長 田中{DUMMY_MARK}",
            languages=[],
            body=f"勤務態度・労働時間を確認した。{DUMMY_MARK}",
        ),
        **overrides,
    )
    return support_interviews.create_interview(worker, data, by=None, today=ctx.base)


def last_interview_days(number: int) -> int:
    """最後の面談が基準日の何日前か。"""
    if number in OVERDUE:
        return 100
    if number in WITHIN_7:
        return 88
    if number in WITHIN_14:
        return 80
    return 20 + (number * 7) % 40  # 期限は 15 日より先（D+31〜D+71）


def seed_interviews(ctx: SeedContext) -> None:
    for number, worker in enumerate(ctx.workers, start=1):
        if number in NOT_RESIDING:
            continue
        first_start = _starts_on(_contract(worker))
        last = ctx.days(-last_interview_days(number))
        history = [last - timedelta(days=180), last - timedelta(days=90), last]
        for on in history:
            if on > first_start:
                _interview(ctx, worker, on)
    # 7 番: 通報「要」の組（通報日 D−5）→ 第4-3号のタスクを起票（T24a）
    reporter = ctx.worker(REPORTED_INTERVIEW)
    _interview(
        ctx,
        reporter,
        ctx.days(REPORT_DAY),
        violation_suspected=True,
        report_required=True,
        report_to="labor_standards_office",
        reported_on=ctx.days(REPORT_DAY),
    )
    # 8 番: 記録してから取り消した組（タスクは自動で戻る）
    voided = _interview(ctx, ctx.worker(VOIDED_INTERVIEW), ctx.days(-3))
    support_interviews.void_interview(voided, by=None, reason=f"別の人の面談だった{DUMMY_MARK}")


# --- 相談 ----------------------------------------------------------------------------------

# (番号, 分類, 分類の記述, 受付の日, [(経過の日, 状態, 追加の欄)])
CONSULTATIONS: tuple[tuple[int, str, str, int, list[tuple[int, str, dict[str, Any]]]], ...] = (
    (
        REPORTED_INTERVIEW,
        "wages",
        "",
        -6,
        [
            (
                REPORT_DAY,
                ConsultationStatus.IN_PROGRESS,
                {
                    "reported": True,
                    "report_to": "labor_standards_office",
                    "reported_on": REPORT_DAY,
                    "hello_work": True,
                    "hello_work_on": REPORT_DAY,
                },
            )
        ],
    ),
    (
        HANDED_OVER,
        "relationships",
        "",
        -40,
        [
            (-35, ConsultationStatus.IN_PROGRESS, {}),
            (
                -30,
                ConsultationStatus.HANDED_OVER,
                {"handed_over_to": f"法テラス{DUMMY_MARK}"},
            ),
        ],
    ),
    (
        REOPENED,
        "daily_life",
        "",
        -50,
        [(-45, ConsultationStatus.RESOLVED, {}), (-10, ConsultationStatus.IN_PROGRESS, {})],
    ),
    (16, "employment", "", -2, []),
    (
        17,
        "health",
        "",
        -30,
        [(-28, ConsultationStatus.IN_PROGRESS, {}), (-20, ConsultationStatus.RESOLVED, {})],
    ),
    (19, "other", f"寮の騒音{DUMMY_MARK}", -25, [(-24, ConsultationStatus.RESOLVED, {})]),
    (20, "wages", "", -12, [(-10, ConsultationStatus.IN_PROGRESS, {})]),
    (22, "daily_life", "", -1, []),
)


def seed_consultations(ctx: SeedContext) -> None:
    for number, category, note, received, entries in CONSULTATIONS:
        worker = ctx.worker(number)
        case = consultations.create_consultation(
            worker,
            ReceptionInput(
                record=_row(
                    ctx,
                    worker,
                    ctx.days(received),
                    kind=SupportItem.CONSULTATION,
                    method="phone",
                    body=f"相談を受けた（{category}）。{DUMMY_MARK}",
                ),
                category=category,
                category_note=note,
            ),
            by=None,
            today=ctx.base,
        )
        for offset, status, extra in entries:
            values: dict[str, Any] = {
                "occurred_on": ctx.days(offset),
                "method": "phone",
                "staff": worker.assigned_staff,
                "body": f"対応の経過。{DUMMY_MARK}",
                "status_after": status,
            }
            for key in ("reported_on", "hello_work_on"):
                if key in extra:
                    extra = extra | {key: ctx.days(extra[key])}
            consultations.add_entry(case, EntryInput(**(values | extra)), by=None, today=ctx.base)


def seed_closed_and_new_report_tasks(ctx: SeedContext) -> None:
    """7 番: 面談の通報で起票した第4-3号のタスクを提出済みにしてから、同じ日の相談の経過を記録する
    → 同じ日付で、閉じたタスクと新しい未完了のタスクが並ぶ（ADR-0012）。"""
    worker = ctx.worker(REPORTED_INTERVIEW)
    [task] = Filing.objects.filter(
        kind=FilingKind.SUPPORT_DIFFICULTY, worker=worker, status=FilingStatus.NOT_STARTED
    )
    filings.transition(task, FilingStatus.SUBMITTED, by=None, submitted_on=ctx.days(-3))


# --- 事前ガイダンス・生活オリエンテーション -------------------------------------------------------


def _checks(
    kind: SupportItem, not_applicable: dict[str, str] | None = None
) -> dict[str, CheckInput]:
    checklist = briefings.checklist_for(kind, date.today())
    result = {code: CheckInput("done") for code in checklist.item_codes}
    for code, reason in (not_applicable or {}).items():
        result[code] = CheckInput("not_applicable", reason)
    return result


def _briefing(
    ctx: SeedContext, number: int, kind: SupportItem, sessions: list[SessionInput], **overrides: Any
) -> Briefing:
    worker = ctx.worker(number)
    confirmations = overrides.pop("confirmations", [])
    values: dict[str, Any] = {
        "kind": kind,
        "method": "in_person",
        "staff": worker.assigned_staff,
        "place": f"受入れ機関の会議室{DUMMY_MARK}",
        "sessions": sessions,
        "checks": _checks(kind),
        "languages": [worker.support_language],
    }
    if kind == SupportItem.PRE_GUIDANCE:
        values |= {"deposit_contract": "none", "foreign_fees": "none"}
    return briefings.create_briefing(
        worker,
        BriefingInput(**(values | overrides)),
        by=None,
        confirmations=confirmations,
        today=ctx.base,
    )


def seed_briefings(ctx: SeedContext) -> None:
    d = ctx.days
    _briefing(
        ctx,
        GUIDANCE_COMPLETE,
        SupportItem.PRE_GUIDANCE,
        [SessionInput(d(-10), time(13), time(16))],
        method="online",
        identity_confirmed=True,
        place=f"オンライン（自機関の事務所）{DUMMY_MARK}",
        foreign_fees="paid",
        fees=[FeeInput(f"送出機関{DUMMY_MARK}", d(-200), 150000, "紹介料 10 万円、研修費 5 万円")],
        fees_agreed=True,
        signed_on=d(-10),
        confirmations=[_pdf("dummy_guidance_signed.pdf")],
    )
    _briefing(
        ctx,
        GUIDANCE_SHORT,
        SupportItem.PRE_GUIDANCE,
        [SessionInput(d(-30), time(14), time(16))],
    )
    _briefing(
        ctx,
        ORIENTATION_NOT_APPLICABLE,
        SupportItem.ORIENTATION,
        [SessionInput(d(-61), time(9), time(13)), SessionInput(d(-60), time(9), time(13))],
        checks=_checks(
            SupportItem.ORIENTATION,
            {
                "4.a.9": f"本人に子どもがいない{DUMMY_MARK}",
                "4.b.6": f"自転車を使わない{DUMMY_MARK}",
            },
        ),
        signed_on=d(-60),
        confirmations=[_pdf("dummy_orientation_signed.pdf")],
    )
    _briefing(
        ctx,
        ORIENTATION_SHORT,
        SupportItem.ORIENTATION,
        [SessionInput(d(-20), time(9), time(12)), SessionInput(d(-20), time(13), time(16))],
    )


# --- 転職支援 ------------------------------------------------------------------------------


def seed_job_changes(ctx: SeedContext) -> None:
    for number, separation, offset in (
        (INVOLUNTARY_JOB_CHANGE, "involuntary", REPORT_DAY),  # 第4-3号のタスクを起票
        (VOLUNTARY_JOB_CHANGE, "voluntary", -2),
    ):
        worker = ctx.worker(number)
        support_records.create_record(
            worker,
            _row(
                ctx,
                worker,
                ctx.days(offset),
                kind=SupportItem.JOB_CHANGE,
                body=f"次の受入れ先の情報を提供し、ハローワークを案内した。{DUMMY_MARK}",
                details={"separation": separation, "activities": ["8.a", "8.b"]},
            ),
            by=None,
            today=ctx.base,
        )


# --- 交付記録・要配慮添付 ------------------------------------------------------------------


def _delivery(ctx: SeedContext, worker: ForeignWorker, **values: Any) -> DocumentDelivery:
    data: dict[str, Any] = {"method": "hand", "delivered_by": "organization", "explained": True}
    return deliveries.create_delivery(
        worker,
        DeliveryInput(**(data | values)),
        by=None,
        files=[_pdf("dummy_signed_copy.pdf")],
        today=ctx.base,
    )


def seed_deliveries(ctx: SeedContext) -> None:
    # 食品の契約のキャリアアッププラン（締結の 5 日前に交付）。15 番と、9 番の新しい契約は未交付
    for number in FOOD_NUMBERS:
        if number == CAREER_PLAN_MISSING:
            continue
        worker = ctx.worker(number)
        contract = _contract(worker)  # 9 番は前の契約（食品）
        _delivery(
            ctx,
            worker,
            kind="career_up_plan",
            delivered_by="host",
            delivered_on=_signed_on(contract) - timedelta(days=5),
            contract=contract,
        )
    first = ctx.worker(1)
    contract = _contract(first)
    _delivery(
        ctx,
        first,
        kind="support_plan_copy",
        delivered_on=_starts_on(contract) - timedelta(days=20),
    )
    for number in HEALTH_DOCUMENTS:
        worker = ctx.worker(number)
        contract = _contract(worker)
        _delivery(
            ctx,
            worker,
            kind="employment_contract_copy",
            delivered_by="host",
            delivered_on=_signed_on(contract),
            contract=contract,
            explained=False,
        )


def seed_health_documents(ctx: SeedContext) -> None:
    """健康診断の書類（要配慮）。1 番は佐藤、2 番は鈴木の担当（担当でない人は 403）。"""
    for number, kinds in (
        (1, ("health_check_record", "health_declaration")),
        (2, ("health_check_record",)),
    ):
        worker = ctx.worker(number)
        for kind in kinds:
            worker_documents.add_documents(
                worker,
                kind,
                [_pdf(f"dummy_{kind}.pdf")],
                retention_until=ctx.days(365),
                by=None,
                today=ctx.base,
            )


# --- 実行 ----------------------------------------------------------------------------------


def seed_support(ctx: SeedContext) -> None:
    seed_plans(ctx)
    seed_interviews(ctx)
    seed_closed_and_new_report_tasks(ctx)
    seed_consultations(ctx)
    seed_briefings(ctx)
    seed_job_changes(ctx)
    seed_deliveries(ctx)
    seed_health_documents(ctx)


def delete_support_data(worker_ids: Iterable[int]) -> int:
    """台帳の外国人に紐づく支援のデータを消す（開発用。外部キーの PROTECT の順に一括削除）。

    作成後に変えない行（ImmutableRow）も、この処理に限り QuerySet の一括削除で消す。
    """
    ids = list(worker_ids)
    if not ids:
        return 0
    records = SupportRecord.objects.filter(worker_id__in=ids)
    record_ids = list(records.values_list("pk", flat=True))
    delivery_ids = list(
        DocumentDelivery.objects.filter(worker_id__in=ids).values_list("pk", flat=True)
    )
    files = Attachment.objects.filter(
        models.Q(
            content_type=ContentType.objects.get_for_model(SupportRecord),
            object_id__in=record_ids,
        )
        | models.Q(
            content_type=ContentType.objects.get_for_model(DocumentDelivery),
            object_id__in=delivery_ids,
        )
        | models.Q(content_type=ContentType.objects.get_for_model(ForeignWorker), object_id__in=ids)
    )
    for attachment in files:
        attachment.file.delete(save=False)
    deleted = files.delete()[0]
    plan_filter = {"version__plan__worker_id__in": ids}
    for queryset in (
        BriefingCheck.objects.filter(briefing__worker_id__in=ids),
        BriefingSession.objects.filter(briefing__worker_id__in=ids),
        BriefingForeignFee.objects.filter(briefing__worker_id__in=ids),
        Briefing.objects.filter(worker_id__in=ids),
        ConsultationEntry.objects.filter(consultation__worker_id__in=ids),
        Consultation.objects.filter(worker_id__in=ids),
        DocumentDelivery.objects.filter(pk__in=delivery_ids),
        SupportRecord.objects.filter(pk__in=record_ids),
        PeriodicInterview.objects.filter(worker_id__in=ids),
        SupportPlanActivity.objects.filter(**plan_filter),
        SupportPlanItem.objects.filter(**plan_filter),
        SupportPlanHousing.objects.filter(**plan_filter),
        SupportPlanConsultation.objects.filter(**plan_filter),
        SupportPlanMunicipalityCheck.objects.filter(**plan_filter),
        SupportPlanVersion.objects.filter(plan__worker_id__in=ids),
        SupportPlan.objects.filter(worker_id__in=ids),
    ):
        deleted += queryset.delete()[0]
    return deleted
