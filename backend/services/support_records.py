"""支援実績の記録（第5章 F05、T18）。共通の欄・種別ごとの欄の検査、取消、集計。

- 上書きしない: 誤りは取消（理由必須）＋新しい記録。添付の誤りは添付ごとの取消
  （services.attachments.void_attachment）。
- 種別ごとの最小限の欄は SupportRecord.details（JSON）に持ち、形は DETAIL_FIELDS で検査する。
- 支援計画の版は実施日から自動で決める（plan_version_for）。計画が無くても記録できる。
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError
from django.core.files import File
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.contracts.models import EmploymentContract
from apps.core.models import Attachment, AttachmentKind, Staff, SupportLanguage, User
from apps.support.models import (
    BodyLanguage,
    Counterpart,
    PeriodicInterview,
    PropertyType,
    RecordMethod,
    SupportPlan,
    SupportPlanVersion,
    SupportRecord,
)
from apps.workers.models import ForeignWorker
from services import attachments, incident_reports, support_plans, workers
from services.support_catalog import SupportItem

# T18 で記録できる種別（定期面談・相談・ガイダンス等は T19〜T21 で専用の画面を足す）
RECORD_KINDS: tuple[SupportItem, ...] = (
    SupportItem.AIRPORT_TRANSFER,
    SupportItem.HOUSING_AND_CONTRACTS,
    SupportItem.PUBLIC_PROCEDURES,
    SupportItem.JAPANESE_LEARNING,
    SupportItem.EXCHANGE,
    SupportItem.JOB_CHANGE,
)

MAX_DURATION_MINUTES = 24 * 60

# 種別ごとに選べる方法の既定（メッセージは相談・苦情だけ、定期面談は電話を除く）
DEFAULT_METHODS: tuple[str, ...] = (RecordMethod.IN_PERSON, RecordMethod.ONLINE, RecordMethod.PHONE)

# この画面で記録する種別ごとの方法（ADR-0014）。送迎・公的手続への同行は対面だけ
# （港・飛行場への送迎、窓口への同行。運用要領 別冊 12〜13頁・21〜23頁）。
METHODS_BY_KIND: dict[SupportItem, tuple[str, ...]] = {
    SupportItem.AIRPORT_TRANSFER: (RecordMethod.IN_PERSON,),
    SupportItem.HOUSING_AND_CONTRACTS: DEFAULT_METHODS,
    SupportItem.PUBLIC_PROCEDURES: (RecordMethod.IN_PERSON,),
    SupportItem.JAPANESE_LEARNING: DEFAULT_METHODS,
    SupportItem.EXCHANGE: DEFAULT_METHODS,
    SupportItem.JOB_CHANGE: DEFAULT_METHODS,
}


def methods_for(kind: SupportItem | str) -> tuple[str, ...]:
    """種別で選べる方法（METHODS_BY_KIND に無い種別は既定）。"""
    return METHODS_BY_KIND.get(SupportItem(kind), DEFAULT_METHODS)


@dataclass(frozen=True)
class DetailField:
    """種別ごとの欄 1 つ。kind は "text"（1 行）/ "choice"（1 つ）/ "multi"（複数）。

    選択肢の値は、計画の行と突き合わせられるよう様式の細目の符号（"3a.a" など）を使う。
    """

    name: str
    label: Any
    kind: str
    required: bool = False
    choices: tuple[tuple[str, Any], ...] = ()
    max_length: int = 100


OTHER = ("other", gettext_lazy("other"))

DETAIL_FIELDS: dict[SupportItem, tuple[DetailField, ...]] = {
    SupportItem.AIRPORT_TRANSFER: (
        DetailField(
            "direction",
            gettext_lazy("direction"),
            "choice",
            required=True,
            choices=(
                ("2.a", gettext_lazy("on entry (airport → host organization or housing)")),
                ("2.b", gettext_lazy("on departure (to the airport)")),
            ),
        ),
        DetailField("airport", gettext_lazy("airport"), "text", required=True),
        DetailField("transport", gettext_lazy("means of transfer"), "text"),
    ),
    SupportItem.HOUSING_AND_CONTRACTS: (
        DetailField(
            "activities",
            gettext_lazy("support given"),
            "multi",
            required=True,
            choices=(
                ("3a.a", gettext_lazy("help finding housing and with the lease")),
                ("3a.b", gettext_lazy("providing housing leased by us")),
                ("3a.c", gettext_lazy("providing company housing")),
                ("3b.a", gettext_lazy("opening a bank account")),
                ("3b.b", gettext_lazy("mobile phone contract")),
                ("3b.c", gettext_lazy("electricity, water and gas")),
                OTHER,
            ),
        ),
        DetailField(
            "property_type",
            gettext_lazy("type of property"),
            "choice",
            choices=tuple((choice.value, choice.label) for choice in PropertyType),
        ),
        # 帳簿④ⅲ「保証人契約の内容」（任意。T44）
        DetailField(
            "guarantor", gettext_lazy("guarantor contract (details)"), "text", max_length=300
        ),
    ),
    SupportItem.PUBLIC_PROCEDURES: (
        DetailField(
            "procedure",
            gettext_lazy("procedure"),
            "text",
            required=True,
        ),
        DetailField("office", gettext_lazy("office"), "text"),
    ),
    SupportItem.JAPANESE_LEARNING: (
        DetailField(
            "ways",
            gettext_lazy("support given"),
            "multi",
            required=True,
            choices=(
                ("5.a", gettext_lazy("information on Japanese classes and schools")),
                ("5.b", gettext_lazy("information on study materials and online courses")),
                ("5.c", gettext_lazy("Japanese lessons with a contracted teacher")),
                OTHER,
            ),
        ),
        # 帳簿④ⅵ「担当者（委託先の講師を含む）」（任意。T44）
        DetailField("instructor", gettext_lazy("contracted teacher (name and title)"), "text"),
    ),
    SupportItem.EXCHANGE: (
        DetailField("event", gettext_lazy("event or activity"), "text", required=True),
        DetailField(
            "exchange_type",
            gettext_lazy("type"),
            "choice",
            choices=(
                ("7.a", gettext_lazy("exchange with local residents")),
                ("7.b", gettext_lazy("local events and Japanese culture")),
            ),
        ),
        DetailField("place", gettext_lazy("place"), "text"),  # 帳簿④ⅷ（任意。T44）
    ),
    SupportItem.JOB_CHANGE: (
        # 離職の区分（非自発的のときだけ第4-3号のタスクを起票する。T24a）。既存の記録は空のまま
        DetailField(
            "separation",
            gettext_lazy("type of separation"),
            "choice",
            required=True,
            choices=(
                ("involuntary", gettext_lazy("involuntary (not the worker's fault)")),
                ("voluntary", gettext_lazy("voluntary")),
            ),
        ),
        DetailField(
            "activities",
            gettext_lazy("support given"),
            "multi",
            required=True,
            choices=(
                ("8.a", gettext_lazy("information on the next employer")),
                ("8.b", gettext_lazy("guidance to Hello Work and accompanying")),
                ("8.c", gettext_lazy("letter of recommendation")),
                ("8.d", gettext_lazy("job placement")),
                ("8.e", gettext_lazy("paid leave for job hunting")),
                ("8.f", gettext_lazy("information on procedures when leaving")),
                ("8.g", gettext_lazy("arranging a substitute supporter")),
                OTHER,
            ),
        ),
        # 帳簿④ⅸ（任意。T44）
        DetailField("place", gettext_lazy("place"), "text"),
        DetailField(
            "candidates",
            gettext_lazy("candidate employers (name, address and contact)"),
            "text",
            max_length=500,
        ),
        DetailField("interpreter", gettext_lazy("interpreter (name and affiliation)"), "text"),
    ),
}


def kind_or_none(value: str) -> SupportItem | None:
    """URL などの文字列から T18 の種別。対象外なら None。"""
    return next((kind for kind in RECORD_KINDS if kind.value == value), None)


@dataclass
class RecordInput:
    """記録の入力（画面のフォームで型だけ整えたもの）。検査は validate で行う。"""

    kind: SupportItem
    occurred_on: datetime.date
    method: str
    staff: Staff | None
    body: str
    body_language: str = BodyLanguage.JA
    start_time: datetime.time | None = None
    duration_minutes: int | None = None
    meeting_url: str = ""
    counterpart: str = Counterpart.WORKER
    counterpart_name: str = ""
    languages: Sequence[SupportLanguage] = ()
    details: dict[str, Any] | None = None
    declined_by_worker: bool = False  # 本人の申出により実施しなかった（T44）


def body_language_for(worker: ForeignWorker) -> str:
    """本文の言語の初期値（支援言語が中国語なら中国語、それ以外は日本語）。"""
    code = worker.support_language.code.lower() if worker.support_language_id else ""
    return BodyLanguage.ZH_HANS if code.startswith("zh") else BodyLanguage.JA


def clean_details(kind: SupportItem, raw: dict[str, Any] | None) -> dict[str, Any]:
    """種別ごとの欄を検査し、保存する形（空の欄は持たない）にする。エラーは "details.<name>"。"""
    raw = raw or {}
    errors: dict[str, list[ValidationError]] = {}
    cleaned: dict[str, Any] = {}
    for spec in DETAIL_FIELDS.get(kind, ()):
        value = raw.get(spec.name)
        allowed = {code for code, _label in spec.choices}
        key = f"details.{spec.name}"
        if spec.kind == "multi":
            values = [str(v) for v in (value or [])]
            if any(v not in allowed for v in values):
                errors[key] = [ValidationError(_("Select a valid choice."), code="invalid_choice")]
            elif values:
                cleaned[spec.name] = [code for code, _label in spec.choices if code in values]
        else:
            text = str(value or "").strip()
            if spec.kind == "choice" and text and text not in allowed:
                errors[key] = [ValidationError(_("Select a valid choice."), code="invalid_choice")]
            elif len(text) > spec.max_length:
                errors[key] = [ValidationError(_("This value is too long."), code="max_length")]
            elif text:
                cleaned[spec.name] = text
        if spec.required and spec.name not in cleaned and key not in errors:
            errors[key] = [ValidationError(_("This field is required."), code="required")]
    if errors:
        raise ValidationError(errors)
    return cleaned


def validate(
    worker: ForeignWorker,
    data: RecordInput,
    *,
    today: datetime.date,
    kinds: Iterable[SupportItem] = RECORD_KINDS,
    methods: Iterable[str] | None = None,
    require_body: bool = True,
) -> dict[str, Any]:
    """入力を検査し、種別ごとの欄を保存する形で返す。エラーは欄の名前ごと。

    kinds・methods は記録できる種別・方法（定期面談は電話を除く。T19）。methods を省くと
    種別ごとの方法（methods_for。ADR-0014）。
    require_body=False は本文を任意にする（事前ガイダンス等は細目のチェックが内容。T21）。
    """
    errors: dict[str, list[ValidationError]] = {}

    def add(key: str, message: str, code: str) -> None:
        errors.setdefault(key, []).append(ValidationError(message, code=code))

    if not workers.is_active(worker):
        add("__all__", _("This foreign worker is deactivated."), "deactivated")
    if data.kind not in tuple(kinds):
        add("kind", _("This kind of support cannot be recorded here."), "kind")
    if data.occurred_on > today:
        add("occurred_on", _("The date cannot be in the future."), "future")
    allowed = tuple(methods) if methods is not None else methods_for(data.kind)
    if data.method not in RecordMethod.values:
        add("method", _("Select a valid choice."), "invalid_choice")
    elif data.method not in allowed:
        add("method", _("This method cannot be used for this kind of support."), "method_for_kind")
    if data.meeting_url and data.method != RecordMethod.ONLINE:
        add("meeting_url", _("Enter the URL only for online support."), "meeting_url")
    if data.staff is None:
        add("staff", _("This field is required."), "required")
    elif not data.staff.is_active:
        add("staff", _("This staff member is no longer active."), "inactive_staff")
    if data.counterpart not in Counterpart.values:
        add("counterpart", _("Select a valid choice."), "invalid_choice")
    if data.counterpart_name and data.counterpart != Counterpart.SUPERVISOR:
        add(
            "counterpart_name",
            _("Enter the name only when the counterpart is the supervisor."),
            "counterpart_name",
        )
    if data.duration_minutes is not None and not 1 <= data.duration_minutes <= MAX_DURATION_MINUTES:
        add(
            "duration_minutes",
            _("Enter the duration in minutes (1 to %(max)s).") % {"max": MAX_DURATION_MINUTES},
            "duration",
        )
    if data.body_language not in BodyLanguage.values:
        add("body_language", _("Select a valid choice."), "invalid_choice")
    if require_body and not data.body.strip():
        add("body", _("This field is required."), "required")
    details: dict[str, Any] = {}
    try:
        details = clean_details(data.kind, data.details)
    except ValidationError as error:
        for key, messages in error.error_dict.items():
            errors.setdefault(key, []).extend(messages)
    if errors:
        raise ValidationError(errors)
    return details


def contract_on(worker: ForeignWorker, on: datetime.date) -> EmploymentContract | None:
    """実施日の計画を選ぶための雇用契約。次の順で選ぶ。

    1. 実施日を期間に含む契約
    2. 実施日より後に始まる契約のうち、最も早く始まるもの（入国時の送迎は契約の開始前に行う）
    3. 実施日より前に終わった契約のうち、最も遅く終わったもの（1・2 が無いときだけ）

    日付の近さだけで選ぶと、前の受入れ機関の古い計画を見てしまうため、これからの契約を先に見る。
    """
    contracts = list(
        EmploymentContract.objects.filter(worker=worker)
        .annotate(first_starts_on=models.Min("versions__starts_on"))
        .select_related("host_organization")
    )

    def starts(contract: Any) -> datetime.date:
        first: datetime.date = contract.first_starts_on or contract.current_starts_on
        return first

    def ends(contract: Any) -> datetime.date | None:
        return min(
            (d for d in (contract.current_ends_on, contract.terminated_on) if d is not None),
            default=None,
        )

    def ends_before(contract: Any) -> bool:
        end = ends(contract)
        return end is not None and end < on

    covering = [c for c in contracts if starts(c) <= on and not ends_before(c)]
    if covering:
        return max(covering, key=starts)
    upcoming = [c for c in contracts if starts(c) > on]
    if upcoming:
        return min(upcoming, key=starts)
    ended = [c for c in contracts if ends_before(c)]
    return max(ended, key=lambda c: ends(c) or on) if ended else None


def plan_version_for(worker: ForeignWorker, on: datetime.date) -> SupportPlanVersion | None:
    """実施日に適用されていた支援計画の版（その日の受入れ機関の計画）。無ければ None。"""
    contract = contract_on(worker, on)
    if contract is None:
        return None
    plan = SupportPlan.objects.filter(
        worker=worker, host_organization_id=contract.host_organization_id, is_template=False
    ).first()
    return support_plans.current_version(plan, on) if plan else None


def validate_files(files: Iterable[File[Any]]) -> list[File[Any]]:
    files = list(files)
    for file in files:
        try:
            attachments.validate_upload(AttachmentKind.SUPPORT_RECORD, file)
        except ValidationError as error:
            raise ValidationError({"files": error.messages}) from error
    return files


def create_record(
    worker: ForeignWorker,
    data: RecordInput,
    *,
    by: User | None,
    files: Iterable[File[Any]] = (),
    today: datetime.date | None = None,
) -> SupportRecord:
    today = today or timezone.localdate()
    details = validate(worker, data, today=today)
    uploads = validate_files(files)
    with transaction.atomic():
        record = save_record(worker, data, details, uploads, by=by)
        incident_reports.raise_tasks(incident_reports.of_record(record), by=by)
        return record


def save_record(
    worker: ForeignWorker,
    data: RecordInput,
    details: dict[str, Any],
    uploads: list[File[Any]],
    *,
    by: User | None,
    interview: PeriodicInterview | None = None,
) -> SupportRecord:
    """検査済みの入力で記録と添付を保存する（呼び出し側のトランザクションの中で呼ぶ）。"""
    record = SupportRecord(
        worker=worker,
        interview=interview,
        kind=data.kind,
        occurred_on=data.occurred_on,
        start_time=data.start_time,
        duration_minutes=data.duration_minutes,
        method=data.method,
        meeting_url=data.meeting_url.strip(),
        staff=data.staff,
        counterpart=data.counterpart,
        counterpart_name=data.counterpart_name.strip(),
        # 帳簿の「担当者の氏名・役職」の当時の役職（T44-D6）
        staff_position=(data.staff.position if data.staff is not None else "")[:50],
        declined_by_worker=data.declined_by_worker,
        body=data.body.strip(),
        body_language=data.body_language,
        details=details,
        plan_version=plan_version_for(worker, data.occurred_on),
        created_by=by,
        updated_by=by,
    )
    record.save()
    record.languages.set(data.languages)
    for file in uploads:
        attachments.create_attachment(
            target=record, kind=AttachmentKind.SUPPORT_RECORD, file=file, created_by=by
        )
    return record


def add_attachments(
    record: SupportRecord, files: Iterable[File[Any]], *, by: User | None
) -> list[Attachment]:
    """記録に添付を足す（取り消した記録・無効化した外国人には足さない）。"""
    if record.voided_at is not None or not workers.is_active(record.worker):
        raise ValidationError(_("Attachments cannot be added to this record."), code="locked")
    uploads = validate_files(files)
    if not uploads:
        raise ValidationError({"files": [_("Select a file.")]})
    with transaction.atomic():
        return [
            attachments.create_attachment(
                target=record, kind=AttachmentKind.SUPPORT_RECORD, file=file, created_by=by
            )
            for file in uploads
        ]


def void_record(
    record: SupportRecord, *, by: User | None, reason: str, as_part: bool = False
) -> incident_reports.Withdrawn:
    """記録を取り消す（打ち消して残し、集計から外す）。正しい記録は新しく足す。

    定期面談の行・相談の受付の行・事前ガイダンス等の行は、それぞれの画面から取り消す
    （support_interviews.void_interview・consultations.void_consultation・briefings.void_briefing
    から as_part=True で呼ぶ）。
    """
    if record.interview_id is not None and not as_part:
        raise ValidationError(
            _("Void the periodic interview as a whole (both rows)."), code="interview"
        )
    if hasattr(record, "consultation") and not as_part:
        raise ValidationError(_("Void the consultation as a whole."), code="consultation")
    if hasattr(record, "briefing") and not as_part:
        raise ValidationError(
            _("Void it on the page of the guidance or orientation."), code="briefing"
        )
    if record.voided_at is not None:
        raise ValidationError(_("This record is already voided."), code="voided")
    if not reason.strip():
        raise ValidationError(_("Enter the reason for voiding."), code="void_reason")
    with transaction.atomic():
        record.voided_at = timezone.now()
        record.voided_by = by
        record.void_reason = reason.strip()
        record.updated_by = by
        record.save(
            update_fields=["voided_at", "voided_by", "void_reason", "updated_by", "updated_at"]
        )
        # 転職支援の記録から起票した第4-3号のタスクの参照を外す（T24a）
        return incident_reports.withdraw(
            [incident_reports.record_key(record)], by=by, reason=reason.strip()
        )


# --- 一覧・表示 -------------------------------------------------------------------------


def records_of(worker: ForeignWorker) -> models.QuerySet[SupportRecord]:
    """外国人の記録（取り消した記録も含む。新しい順）。"""
    return (
        SupportRecord.objects.filter(worker=worker)
        .select_related("staff", "plan_version", "consultation", "briefing")
        .prefetch_related("languages")
        .order_by("-occurred_on", models.F("start_time").desc(nulls_last=True), "-pk")
    )


def filter_records(
    queryset: models.QuerySet[SupportRecord],
    *,
    kind: str = "",
    date_from: datetime.date | None = None,
    date_to: datetime.date | None = None,
    staff_id: int | None = None,
) -> models.QuerySet[SupportRecord]:
    if kind:
        queryset = queryset.filter(kind=kind)
    if date_from:
        queryset = queryset.filter(occurred_on__gte=date_from)
    if date_to:
        queryset = queryset.filter(occurred_on__lte=date_to)
    if staff_id:
        queryset = queryset.filter(staff_id=staff_id)
    return queryset


def details_display(record: SupportRecord) -> list[tuple[str, str]]:
    """種別ごとの欄の（見出し, 値）。選択肢は表示名にする。"""
    rows = []
    for spec in DETAIL_FIELDS.get(SupportItem(record.kind), ()):
        value = record.details.get(spec.name)
        if not value:
            continue
        labels = {code: str(label) for code, label in spec.choices}
        if spec.kind == "multi":
            text = "、".join(labels.get(code, code) for code in value)
        elif spec.kind == "choice":
            text = labels.get(value, value)
        else:
            text = str(value)
        rows.append((str(spec.label), text))
    return rows


# --- 集計（T24 の月次の集計・定期報告の材料） -------------------------------------------------


@dataclass(frozen=True)
class KindSummary:
    count: int
    last_on: datetime.date


def counts(
    worker_ids: Iterable[int], date_from: datetime.date, date_to: datetime.date
) -> dict[tuple[int, str], KindSummary]:
    """外国人×種別ごとの、期間内（両端を含む）の件数と最後の実施日。取り消した記録は数えない。

    記録の無い組は含めない（呼び出し側で 0 件として扱う）。
    """
    rows = (
        SupportRecord.objects.filter(
            worker_id__in=list(worker_ids),
            occurred_on__gte=date_from,
            occurred_on__lte=date_to,
            voided_at__isnull=True,
        )
        .values("worker_id", "kind")
        .annotate(count=models.Count("pk"), last_on=models.Max("occurred_on"))
        .order_by()
    )
    return {
        (row["worker_id"], row["kind"]): KindSummary(count=row["count"], last_on=row["last_on"])
        for row in rows
    }
