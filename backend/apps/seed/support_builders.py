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
from dataclasses import replace
from datetime import date, time, timedelta
from decimal import Decimal
from typing import Any

from django.contrib.contenttypes.models import ContentType
from django.core.files.base import ContentFile
from django.db import models

from apps.contracts.models import EmploymentContract
from apps.core.models import Attachment
from apps.filings.models import Filing, FilingKind, FilingStatus
from apps.forms_engine.models import (
    GeneratedDocument,
    GeneratedDocumentSignature,
    SubmissionBundle,
    SubmissionDraft,
    SubmissionDraftFile,
)
from apps.hosts.models import HostDocument, HostOrganization
from apps.seed.context import DUMMY_MARK, SeedContext, dummy_pdf
from apps.support.models import (
    Briefing,
    BriefingCheck,
    BriefingForeignFee,
    BriefingSession,
    Consultation,
    ConsultationEntry,
    ConsultationStatus,
    DocumentDelivery,
    InterviewAnswer,
    InterviewAnswerSet,
    InterviewNonconformity,
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
    interview_answers,
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
NO_ANSWERS = frozenset({3, 19})  # 最後の定期面談も報告書の回答が無い（T35 より前の記録。T35-D3）
ONLINE_INTERVIEW = 5  # 最後の定期面談の本人用の行がオンライン（録画の保存先つき。T35-D8）


def _pdf(name: str) -> ContentFile[bytes]:
    return ContentFile(dummy_pdf(name.removeprefix("dummy_").removesuffix(".pdf")), name=name)


def _contract(worker: ForeignWorker) -> EmploymentContract:
    """最初の雇用契約（転籍の人は前の契約）。"""
    return EmploymentContract.objects.filter(worker=worker).order_by("current_starts_on").first()  # type: ignore[return-value]


def _starts_on(contract: EmploymentContract) -> date:
    return contract.versions.order_by("version").first().starts_on  # type: ignore[union-attr]


def _signed_on(contract: EmploymentContract) -> date:
    return contract.versions.order_by("version").first().signed_on  # type: ignore[union-attr,return-value]


# --- 支援計画 --------------------------------------------------------------------------------


def fill_plan(content: support_plans.PlanContent, applies_from: date) -> None:
    """第1-17号で空にならないように、計画の欄を埋める（実施予定時間・送迎・住居の概要・相談の受付・
    共生施策の確認・作成責任者。2026-10-20 の生成前の検査の警告に合わせた）。"""
    for item, hours in ((SupportItem.PRE_GUIDANCE, "3"), (SupportItem.ORIENTATION, "8")):
        content.items[item] = replace(content.items[item], planned_hours=Decimal(hours))
    pickup = content.activities["2.a"]
    content.activities["2.a"] = replace(
        pickup,
        methods=sorted({*pickup.methods, "arrival_airport", "transport"}),
        method_notes={"arrival_airport": f"成田国際空港{DUMMY_MARK}", "transport": "社用車"},
    )
    content.housing = support_plans.HousingContent(
        timing="secured",
        room_area_ok=True,
        occupants=2,
        bedroom_area_ok=True,
        rent_collected=True,
        monthly_rent=25000,
        property_type="leased",
        rent_explanation=f"近隣の同程度の住居の家賃以下の額であることを説明{DUMMY_MARK}",
    )
    content.consultation = support_plans.ConsultationContent(
        hours={day: ["09:00", "18:00"] for day in ("mon", "tue", "wed", "thu", "fri")},
        methods=["in_person", "phone", "email"],
        phone="000-0000-9001",
        email="soudan@example.com",
        emergency_methods=["phone"],
        emergency_phone="000-0000-9002",
    )
    checked_on = applies_from - timedelta(days=7)
    content.municipality_checks = [
        support_plans.MunicipalityContent(
            place="site",
            prefecture="東京都",
            municipality=f"サンプル区{DUMMY_MARK}",
            checked_on=checked_on,
            method="website",
        ),
        support_plans.MunicipalityContent(
            place="residence",
            same_as_site=True,
            prefecture="東京都",
            municipality=f"サンプル区{DUMMY_MARK}",
            checked_on=checked_on,
            method="website",
        ),
    ]
    content.responsible_name = f"代表理事　代表 花子{DUMMY_MARK}"


def seed_plans(ctx: SeedContext) -> None:
    for worker in ctx.workers:
        for contract in EmploymentContract.objects.filter(worker=worker).order_by(
            "current_starts_on"
        ):
            applies_from = _starts_on(contract) - timedelta(days=30)
            content = support_plans.content_for_worker(worker)
            content.created_on = applies_from  # 第1-17号の作成日（T31）
            fill_plan(content, applies_from)
            support_plans.create_plan(
                worker,
                contract.host_organization,
                applies_from=applies_from,
                by=None,
                content=content,
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


def _answers(on: date, **overrides: Any) -> interview_answers.AnswerSetInput:
    """報告書の回答（全部「問題無」。overrides で問題のある質問などを足す）。"""
    values: dict[str, Any] = {
        "answers": interview_answers.all_no(on),
        "notable": f"特になし。{DUMMY_MARK}",
    }
    return interview_answers.AnswerSetInput(**(values | overrides))


def _interview(
    ctx: SeedContext,
    worker: ForeignWorker,
    on: date,
    *,
    with_answers: bool = True,
    self_overrides: dict[str, Any] | None = None,
    supervisor_overrides: dict[str, Any] | None = None,
    nonconformity: interview_answers.NonconformityInput | None = None,
    **overrides: Any,
) -> Any:
    """面談の組。with_answers=False は T35 より前の記録（報告書の回答が無い。T35-D3）。"""
    self_row = _row(ctx, worker, on, **(overrides.pop("self_row", {})))
    supervisor_row = _row(
        ctx,
        worker,
        on,
        counterpart_name=f"工場長 田中{DUMMY_MARK}",
        languages=[],
        body=f"勤務態度・労働時間を確認した。{DUMMY_MARK}",
    )
    answers: dict[str, Any] = {}
    if with_answers:
        answers = {
            "self_answers": _answers(on, **(self_overrides or {})),
            "supervisor_answers": _answers(
                on,
                supervisor_position=f"工場長{DUMMY_MARK}",
                supervisor_department=f"製造部{DUMMY_MARK}",
                **(supervisor_overrides or {}),
            ),
            "nonconformity": nonconformity or interview_answers.NonconformityInput(exists=False),
        }
    data = InterviewInput(self_row=self_row, supervisor_row=supervisor_row, **answers, **overrides)
    return support_interviews.create_interview(
        worker, data, by=None, today=ctx.base, without_answers=not with_answers
    )


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
            if on <= first_start:
                continue
            # 古い組と NO_ANSWERS の番号の最後の組は、T35 より前の記録（回答なし）の場面
            with_answers = on == last and number not in NO_ANSWERS
            if on == last and number == ONLINE_INTERVIEW:
                _interview(
                    ctx,
                    worker,
                    on,
                    self_row={"method": "online", "meeting_url": "https://meet.example.com/dummy"},
                    self_overrides={
                        "recording_location": f"社内の共有フォルダ（面談録画）{DUMMY_MARK}"
                    },
                )
                continue
            _interview(ctx, worker, on, with_answers=with_answers)
    # 7 番: 通報「要」の組（通報日 D−5）→ 第4-3号のタスクを起票（T24a）。
    # ⑥ 基準不適合等も「有り」で、同じ事由として 1 件のタスクにまとまる（T35-D5）
    reporter = ctx.worker(REPORTED_INTERVIEW)
    problem = f"残業代が契約どおりに支払われていない。{DUMMY_MARK}"
    answers = interview_answers.all_no(ctx.days(REPORT_DAY))
    answers["2.1"] = interview_answers.AnswerInput(True, problem)
    _interview(
        ctx,
        reporter,
        ctx.days(REPORT_DAY),
        violation_suspected=True,
        report_required=True,
        report_to="labor_standards_office",
        reported_on=ctx.days(REPORT_DAY),
        self_overrides={"answers": answers},
        supervisor_overrides={"answers": dict(answers)},
        nonconformity=interview_answers.NonconformityInput(
            exists=True,
            occurred_on=ctx.days(REPORT_DAY - 30),
            content=f"所定時間外労働の割増賃金の一部が支払われていない。{DUMMY_MARK}",
            worker_guidance="guided",
            guided_to=f"労働基準監督署{DUMMY_MARK}",
            host_notified=True,
            notified_on=ctx.days(REPORT_DAY),
            notified_to=f"代表取締役 代表 一郎{DUMMY_MARK}",
            immigration_guided=True,
        ),
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
    ctx: SeedContext, kind: SupportItem, not_applicable: dict[str, str] | None = None
) -> dict[str, CheckInput]:
    checklist = briefings.checklist_for(kind, ctx.base)
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
        "checks": _checks(ctx, kind),
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
            ctx,
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
                examined_on=ctx.days(
                    -20
                ),  # 受診日（認定は 3 か月、変更は 1 年以内。修正 1 回目 B8）
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


def _delete_document_files(
    documents: models.QuerySet[GeneratedDocument], files: models.QuerySet[Attachment]
) -> int:
    """生成物の署名の紐づけ・添付とファイルを消す（生成物の行は呼び出し側が後で消す）。"""
    deleted = GeneratedDocumentSignature.objects.filter(document__in=documents).delete()[0]
    files = files | Attachment.objects.filter(
        content_type=ContentType.objects.get_for_model(GeneratedDocument),
        object_id__in=documents.values("pk"),
    )
    for document in documents:
        document.pdf_file.delete(save=False)
        document.filled_file.delete(save=False)
    for attachment in files:
        attachment.file.delete(save=False)
    return deleted + files.delete()[0]


def delete_host_documents(host_ids: Iterable[int]) -> int:
    """ダミーの受入れ機関の帳票（外国人が 1 人に決まらない第5-4号。T35）を消す。"""
    documents = GeneratedDocument.objects.filter(
        content_type=ContentType.objects.get_for_model(HostOrganization),
        object_id__in=list(host_ids),
    )
    deleted = _delete_document_files(documents, Attachment.objects.none())
    return deleted + documents.delete()[0]


def delete_host_store(host_ids: Iterable[int], site_ids: Iterable[int]) -> int:
    """受入れ機関・事業所の書類の保管庫（T42 の追加）の行と添付を消す（画面で足したものも）。"""
    documents = HostDocument.objects.filter(
        models.Q(host_organization_id__in=list(host_ids)) | models.Q(site_id__in=list(site_ids))
    )
    files = Attachment.objects.filter(
        content_type=ContentType.objects.get_for_model(HostDocument),
        object_id__in=documents.values("pk"),
    )
    for attachment in files:
        attachment.file.delete(save=False)
    deleted = files.delete()[0]
    return deleted + documents.delete()[0]


def delete_submission_data(worker_ids: Iterable[int]) -> int:
    """統合 PDF の生成履歴と下書き（アップロードの行・添付）を消す（T42 とその直し）。"""
    ids = list(worker_ids)
    drafts = SubmissionDraft.objects.filter(worker_id__in=ids)
    files = Attachment.objects.filter(
        content_type=ContentType.objects.get_for_model(SubmissionDraft),
        object_id__in=drafts.values("pk"),
    )
    # 下書きの行が添付を PROTECT で参照するので、行 → 添付 → 下書きの順
    deleted = SubmissionDraftFile.objects.filter(draft__in=drafts).delete()[0]
    for attachment in files:
        attachment.file.delete(save=False)
    deleted += files.delete()[0]
    deleted += drafts.delete()[0]
    bundles = SubmissionBundle.objects.filter(worker_id__in=ids)
    for bundle in bundles:
        bundle.file.delete(save=False)
    return deleted + bundles.delete()[0]


def delete_support_data(worker_ids: Iterable[int]) -> int:
    """台帳の外国人に紐づく支援のデータを消す（開発用。外部キーの PROTECT の順に一括削除）。

    作成後に変えない行（ImmutableRow）も、この処理に限り QuerySet の一括削除で消す。
    """
    ids = list(worker_ids)
    if not ids:
        return 0
    deleted_submission = delete_submission_data(ids)
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
    # 生成した帳票（T29〜T33）: 署名の紐づけ → 添付・ファイル → 生成物（交付記録の後）の順に消す。
    # 生成物と紐づけはモデルの delete() では消せないので、開発用のこの処理に限り一括で消す
    documents = GeneratedDocument.objects.filter(worker_id__in=ids)
    deleted = _delete_document_files(documents, files)
    plan_filter = {"version__plan__worker_id__in": ids}
    for queryset in (
        InterviewAnswer.objects.filter(answer_set__record__worker_id__in=ids),
        InterviewAnswerSet.objects.filter(record__worker_id__in=ids),
        InterviewNonconformity.objects.filter(interview__worker_id__in=ids),
        BriefingCheck.objects.filter(briefing__worker_id__in=ids),
        BriefingSession.objects.filter(briefing__worker_id__in=ids),
        BriefingForeignFee.objects.filter(briefing__worker_id__in=ids),
        Briefing.objects.filter(worker_id__in=ids),
        ConsultationEntry.objects.filter(consultation__worker_id__in=ids),
        Consultation.objects.filter(worker_id__in=ids),
        DocumentDelivery.objects.filter(pk__in=delivery_ids),
        GeneratedDocument.objects.filter(worker_id__in=ids),
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
    return deleted + deleted_submission
