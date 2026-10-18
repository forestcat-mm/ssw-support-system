"""事前ガイダンス・生活オリエンテーション（第5章 F05、第6章「署名を要する書類の扱い」、T21）。

ADR-0009。
- 記録は支援実績の 1 行（種別 1 か 4、相手は本人）＋ Briefing。実施日・開始時刻は最初の
  実施日時の行、所要時間は全行の合計を支援実績の行に写す（一覧と T24 の集計は、
  これまでどおり支援実績の行を見る）。
- チェックリストは services/data/briefing_checklists.json の版。細目はすべて必須で、「説明した」か
  「該当なし」（理由が必須）を付ける。欄は配下の細目がすべてどちらかなら完了。
- 所要時間・未チェック・署名済みの確認書・費用の確認は警告にする（保存は止めない）。
- 事前ガイダンスでは、保証金・違約金の契約と、外国の機関に支払った費用を構造化して保存する
  （在留諸申請の申請書の欄と段階4の一括申請 Excel の元。ADR-0009）。
- 取消は支援実績の行の取消（理由必須）。チェック・実施日時・費用の行は残る。訂正は新しい記録。
"""

from __future__ import annotations

import datetime
import json
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from django.core.exceptions import ValidationError
from django.core.files import File
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from apps.core.models import Attachment, AttachmentKind, Staff, SupportLanguage, User
from apps.support.models import (
    BodyLanguage,
    Briefing,
    BriefingCheck,
    BriefingForeignFee,
    BriefingSession,
    CheckStatus,
    Counterpart,
    DepositContract,
    ForeignFees,
    RecordMethod,
    SupportPlanItem,
)
from apps.workers.models import ForeignWorker
from services import attachments, support_records, workers
from services.support_catalog import ACTIVITY_LABELS, SupportItem
from services.support_records import RecordInput

BRIEFING_KINDS: tuple[SupportItem, ...] = (SupportItem.PRE_GUIDANCE, SupportItem.ORIENTATION)

# 方法: 事前ガイダンスは対面かテレビ電話等（本人確認のうえ）。生活オリエンテーションは動画視聴も可
METHODS: dict[SupportItem, tuple[str, ...]] = {
    SupportItem.PRE_GUIDANCE: (RecordMethod.IN_PERSON, RecordMethod.ONLINE),
    SupportItem.ORIENTATION: (RecordMethod.IN_PERSON, RecordMethod.ONLINE, RecordMethod.VIDEO),
}

CONFIRMATION_KINDS: dict[SupportItem, AttachmentKind] = {
    SupportItem.PRE_GUIDANCE: AttachmentKind.PRE_GUIDANCE_CONFIRMATION,
    SupportItem.ORIENTATION: AttachmentKind.ORIENTATION_CONFIRMATION,
}

FORM_ROWS = 3  # 確認書の実施日時の欄の行数（第5-8号・5-9号）


# --- チェックリストの版（データ） ------------------------------------------------------------


@dataclass(frozen=True)
class ChecklistItem:
    code: str  # 例 "4.a.1"
    label: str


@dataclass(frozen=True)
class ChecklistSection:
    """欄（確認書の項目）。符号と文言は支援計画の細目（support_plan_1-17.json）と同じ。"""

    code: str  # 例 "4.a"
    title: str
    form_number: int | None  # 確認書の番号（確認書に無い欄は None）
    pages: str
    items: tuple[ChecklistItem, ...]


@dataclass(frozen=True)
class DurationRule:
    guideline_minutes: int  # 目安（事前ガイダンス）・必要な時間（生活オリエンテーション）
    guideline_kind: str  # "about"（程度）/ "at_least"（以上）
    minimum_minutes: int  # 下限（同一機関で引き続き雇用する場合でも、これ未満は不適切）


@dataclass(frozen=True)
class Checklist:
    version: str
    effective_from: datetime.date
    kind: SupportItem
    form: str
    duration: DurationRule
    sections: tuple[ChecklistSection, ...]

    @property
    def items(self) -> tuple[ChecklistItem, ...]:
        return tuple(item for section in self.sections for item in section.items)

    @property
    def item_codes(self) -> tuple[str, ...]:
        return tuple(item.code for item in self.items)


@dataclass(frozen=True)
class ChecklistVersion:
    id: str
    effective_from: datetime.date
    sources: tuple[dict[str, Any], ...]
    checklists: dict[SupportItem, Checklist]


DATA_FILE = Path(__file__).resolve().parent / "data" / "briefing_checklists.json"


def _load(path: Path = DATA_FILE) -> tuple[ChecklistVersion, ...]:
    data = json.loads(path.read_text(encoding="utf-8"))
    versions = []
    for raw in data["versions"]:
        effective_from = datetime.date.fromisoformat(raw["effective_from"])
        checklists = {}
        for kind_value, spec in raw["kinds"].items():
            kind = SupportItem(kind_value)
            checklists[kind] = Checklist(
                version=raw["id"],
                effective_from=effective_from,
                kind=kind,
                form=spec["form"],
                duration=DurationRule(**spec["duration"]),
                sections=tuple(
                    ChecklistSection(
                        code=section["code"],
                        title=ACTIVITY_LABELS[section["code"]],
                        form_number=section["form_number"],
                        pages=section["pages"],
                        items=tuple(ChecklistItem(**item) for item in section["items"]),
                    )
                    for section in spec["sections"]
                ),
            )
        versions.append(
            ChecklistVersion(
                id=raw["id"],
                effective_from=effective_from,
                sources=tuple(raw["sources"]),
                checklists=checklists,
            )
        )
    return tuple(sorted(versions, key=lambda version: version.effective_from))


VERSIONS: tuple[ChecklistVersion, ...] = _load()


def checklist_for(kind: SupportItem | str, on: datetime.date) -> Checklist:
    """その日に適用されていた版のチェックリスト（適用開始より前の日なら最も古い版）。"""
    applied = [version for version in VERSIONS if version.effective_from <= on]
    version = applied[-1] if applied else VERSIONS[0]
    return version.checklists[SupportItem(kind)]


def checklist_of(briefing: Briefing) -> Checklist:
    """記録に保存した版のチェックリスト。"""
    version = next(version for version in VERSIONS if version.id == briefing.checklist_version)
    return version.checklists[SupportItem(briefing.record.kind)]


# --- 入力と検査 -------------------------------------------------------------------------


@dataclass
class SessionInput:
    occurred_on: datetime.date
    start_time: datetime.time
    end_time: datetime.time

    @property
    def minutes(self) -> int:
        return (self.end_time.hour * 60 + self.end_time.minute) - (
            self.start_time.hour * 60 + self.start_time.minute
        )


@dataclass
class FeeInput:
    organization_name: str
    paid_on: datetime.date | None
    amount_jpy: int | None
    breakdown: str


@dataclass
class CheckInput:
    status: str
    reason: str = ""


@dataclass
class BriefingInput:
    """記録の入力。事前ガイダンスだけの欄（本人確認・保証金・費用）は生活オリエンテーションでは無視する。"""

    kind: SupportItem
    method: str
    staff: Staff | None
    place: str
    sessions: list[SessionInput]
    checks: dict[str, CheckInput]
    languages: Sequence[SupportLanguage] = ()
    meeting_url: str = ""
    body: str = ""
    body_language: str = BodyLanguage.JA
    interpreter: str = ""
    legal_info_provider: str = ""
    continuing_employment: bool = False
    identity_confirmed: bool = False
    deposit_contract: str = ""
    deposit_collector: str = ""
    deposit_detail: str = ""
    foreign_fees: str = ""
    fees: list[FeeInput] = field(default_factory=list)
    fees_agreed: bool | None = None
    signed_on: datetime.date | None = None


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


def _record_input(data: BriefingInput) -> RecordInput:
    """支援実績の行（実施日・開始時刻は最初の実施日時、所要時間は合計）。"""
    first = min(data.sessions, key=lambda s: (s.occurred_on, s.start_time))
    return RecordInput(
        kind=data.kind,
        occurred_on=first.occurred_on,
        start_time=first.start_time,
        duration_minutes=sum(session.minutes for session in data.sessions),
        method=data.method,
        meeting_url=data.meeting_url,
        staff=data.staff,
        counterpart=Counterpart.WORKER,
        languages=data.languages,
        body=data.body,
        body_language=data.body_language,
    )


def _check_sessions(errors: _Errors, sessions: list[SessionInput], today: datetime.date) -> None:
    if not sessions:
        errors.add("sessions", _("Enter at least one date and time."), "required")
        return
    for index, session in enumerate(sessions):
        if session.occurred_on > today:
            errors.add(
                f"sessions.{index}.occurred_on", _("The date cannot be in the future."), "future"
            )
        if session.minutes <= 0:
            errors.add(
                f"sessions.{index}.end_time",
                _("Enter an end time later than the start time."),
                "end_before_start",
            )
    ordered = sorted(
        enumerate(sessions), key=lambda pair: (pair[1].occurred_on, pair[1].start_time)
    )
    for (_previous_index, previous), (index, session) in zip(ordered, ordered[1:], strict=False):
        if session.occurred_on == previous.occurred_on and session.start_time < previous.end_time:
            errors.add(
                f"sessions.{index}.start_time",
                _("This overlaps another date and time."),
                "overlap",
            )


def _check_items(errors: _Errors, checklist: Checklist, checks: dict[str, CheckInput]) -> None:
    codes = set(checklist.item_codes)
    for code, check in checks.items():
        if code not in codes:
            errors.add("checks", _("Select a valid choice."), "invalid_choice")
        elif check.status not in CheckStatus.values:
            errors.add(f"checks.{code}", _("Select a valid choice."), "invalid_choice")
        elif check.status == CheckStatus.NOT_APPLICABLE and not check.reason.strip():
            errors.add(
                f"checks.{code}", _("Enter the reason why it is not applicable."), "reason_required"
            )


def _check_fees(errors: _Errors, data: BriefingInput, today: datetime.date) -> None:
    """保証金・違約金の契約と、外国の機関に支払った費用（事前ガイダンスだけ）。"""
    if data.deposit_contract not in DepositContract.values:
        errors.add("deposit_contract", _("Select whether there is such a contract."), "required")
    elif data.deposit_contract == DepositContract.EXISTS:
        if not data.deposit_collector.strip():
            errors.add("deposit_collector", _("This field is required."), "required")
        if not data.deposit_detail.strip():
            errors.add("deposit_detail", _("This field is required."), "required")
    elif data.deposit_collector.strip() or data.deposit_detail.strip():
        errors.add(
            "deposit_collector",
            _("Enter this only when there is such a contract."),
            "deposit_without_contract",
        )

    if data.foreign_fees not in ForeignFees.values:
        errors.add("foreign_fees", _("Select whether fees were paid."), "required")
    elif data.foreign_fees == ForeignFees.PAID:
        if not data.fees:
            errors.add("fees", _("Enter at least one fee paid."), "required")
        if data.fees_agreed is None:
            errors.add(
                "fees_agreed",
                _("Select whether the worker understood and agreed on them."),
                "required",
            )
    else:
        if data.fees:
            errors.add("fees", _("Enter fees only when they were paid."), "fees_without_payment")
        if data.fees_agreed is not None:
            errors.add(
                "fees_agreed", _("Enter this only when fees were paid."), "agreed_without_payment"
            )
    for index, fee in enumerate(data.fees):
        prefix = f"fees.{index}"
        if not fee.organization_name.strip():
            errors.add(f"{prefix}.organization_name", _("This field is required."), "required")
        if fee.paid_on is None:
            errors.add(f"{prefix}.paid_on", _("This field is required."), "required")
        elif fee.paid_on > today:
            errors.add(f"{prefix}.paid_on", _("The date cannot be in the future."), "future")
        if fee.amount_jpy is None or fee.amount_jpy < 1:
            errors.add(f"{prefix}.amount_jpy", _("Enter the amount in yen."), "required")
        if not fee.breakdown.strip():
            errors.add(f"{prefix}.breakdown", _("This field is required."), "required")


def _check_signed_on(
    errors: _Errors,
    signed_on: datetime.date | None,
    *,
    last_on: datetime.date | None,
    has_confirmation: bool,
    today: datetime.date,
) -> None:
    if signed_on is None:
        if has_confirmation:
            errors.add("signed_on", _("Enter the date of the signature."), "required")
        return
    if not has_confirmation:
        errors.add("confirmation", _("Attach the signed confirmation."), "required")
    if signed_on > today:
        errors.add("signed_on", _("The date cannot be in the future."), "future")
    elif last_on is not None and signed_on < last_on:
        errors.add(
            "signed_on",
            _("The date of the signature cannot be before the last date of the explanation."),
            "signed_before",
        )


def validate(
    worker: ForeignWorker,
    data: BriefingInput,
    *,
    today: datetime.date,
    has_confirmation: bool = False,
) -> tuple[RecordInput, Checklist]:
    """入力を検査し、支援実績の行と、適用するチェックリストを返す。

    エラーのキーは欄の名前、または "sessions.0.end_time"・"checks.4.a.1"・"fees.0.paid_on"。
    """
    errors = _Errors()
    if data.kind not in BRIEFING_KINDS:
        errors.add("kind", _("This kind of support cannot be recorded here."), "kind")
        errors.raise_if_any()
    _check_sessions(errors, data.sessions, today)
    if not data.sessions:
        errors.raise_if_any()
    record = _record_input(data)
    try:
        # 所要時間は合計なので 1 日の上限で見ない（行ごとに終了 > 開始を見ている）
        support_records.validate(
            worker,
            replace(record, duration_minutes=None),
            today=today,
            kinds=(data.kind,),
            methods=METHODS[data.kind],
            require_body=False,
        )
    except ValidationError as error:
        for key, messages in error.error_dict.items():
            # 実施日は実施日時の行で見ている
            if key != "occurred_on":
                errors.items.setdefault(key, []).extend(messages)
    if not data.place.strip():
        errors.add("place", _("This field is required."), "required")
    if (
        data.kind == SupportItem.PRE_GUIDANCE
        and data.method == RecordMethod.ONLINE
        and not data.identity_confirmed
    ):
        errors.add(
            "identity_confirmed",
            _("Online guidance must be given after confirming the identity of the worker."),
            "identity",
        )
    checklist = checklist_for(data.kind, record.occurred_on)
    _check_items(errors, checklist, data.checks)
    if data.kind == SupportItem.PRE_GUIDANCE:
        _check_fees(errors, data, today)
    _check_signed_on(
        errors,
        data.signed_on,
        last_on=max(session.occurred_on for session in data.sessions),
        has_confirmation=has_confirmation,
        today=today,
    )
    errors.raise_if_any()
    return record, checklist


def _validate_confirmations(kind: SupportItem, files: Iterable[File[Any]]) -> list[File[Any]]:
    files = list(files)
    for file in files:
        try:
            attachments.validate_upload(CONFIRMATION_KINDS[kind], file)
        except ValidationError as error:
            raise ValidationError({"confirmation": error.messages}) from error
    return files


# --- 作成・署名・取消 ---------------------------------------------------------------------


def create_briefing(
    worker: ForeignWorker,
    data: BriefingInput,
    *,
    by: User | None,
    confirmations: Iterable[File[Any]] = (),
    today: datetime.date | None = None,
) -> Briefing:
    """記録する（支援実績の行・Briefing・実施日時・チェック・費用・署名済みの確認書）。"""
    today = today or timezone.localdate()
    confirmations = list(confirmations)
    record_input, checklist = validate(
        worker, data, today=today, has_confirmation=bool(confirmations)
    )
    uploads = _validate_confirmations(data.kind, confirmations)
    is_guidance = data.kind == SupportItem.PRE_GUIDANCE
    online = data.method == RecordMethod.ONLINE
    paid = is_guidance and data.foreign_fees == ForeignFees.PAID
    with transaction.atomic():
        record = support_records.save_record(worker, record_input, {}, [], by=by)
        briefing = Briefing(
            record=record,
            worker=worker,
            checklist_version=checklist.version,
            place=data.place.strip(),
            interpreter=data.interpreter.strip(),
            legal_info_provider="" if is_guidance else data.legal_info_provider.strip(),
            continuing_employment=data.continuing_employment,
            identity_confirmed=is_guidance and online and data.identity_confirmed,
            deposit_contract=data.deposit_contract if is_guidance else "",
            deposit_collector=data.deposit_collector.strip() if is_guidance else "",
            deposit_detail=data.deposit_detail.strip() if is_guidance else "",
            foreign_fees=data.foreign_fees if is_guidance else "",
            fees_agreed=data.fees_agreed if paid else None,
            signed_on=data.signed_on,
            created_by=by,
            updated_by=by,
        )
        briefing.save()
        for session in sorted(data.sessions, key=lambda s: (s.occurred_on, s.start_time)):
            BriefingSession(
                briefing=briefing,
                occurred_on=session.occurred_on,
                start_time=session.start_time,
                end_time=session.end_time,
                created_by=by,
                updated_by=by,
            ).save()
        for code in checklist.item_codes:  # データの順で保存する
            check = data.checks.get(code)
            if check is None:
                continue
            not_applicable = check.status == CheckStatus.NOT_APPLICABLE
            BriefingCheck(
                briefing=briefing,
                code=code,
                status=check.status,
                reason=check.reason.strip() if not_applicable else "",
                created_by=by,
                updated_by=by,
            ).save()
        for fee in data.fees if paid else []:
            assert fee.paid_on is not None and fee.amount_jpy is not None
            BriefingForeignFee(
                briefing=briefing,
                organization_name=fee.organization_name.strip(),
                paid_on=fee.paid_on,
                amount_jpy=fee.amount_jpy,
                breakdown=fee.breakdown.strip(),
                created_by=by,
                updated_by=by,
            ).save()
        for file in uploads:
            attachments.create_attachment(
                target=record, kind=CONFIRMATION_KINDS[data.kind], file=file, created_by=by
            )
    return briefing


def add_confirmation(
    briefing: Briefing,
    files: Iterable[File[Any]],
    *,
    signed_on: datetime.date | None,
    by: User | None,
    today: datetime.date | None = None,
) -> list[Attachment]:
    """署名済みの確認書を添付し、署名日を記録する（後から署名を受けたとき）。"""
    today = today or timezone.localdate()
    record = briefing.record
    if record.voided_at is not None or not workers.is_active(record.worker):
        raise ValidationError(_("Attachments cannot be added to this record."), code="locked")
    files = list(files)
    errors = _Errors()
    if not files:
        errors.add("confirmation", _("Select a file."), "required")
    if signed_on is None:
        errors.add("signed_on", _("Enter the date of the signature."), "required")
    else:
        _check_signed_on(
            errors,
            signed_on,
            last_on=last_session_on(briefing),
            has_confirmation=bool(files),
            today=today,
        )
    errors.raise_if_any()
    kind = SupportItem(record.kind)
    uploads = _validate_confirmations(kind, files)
    with transaction.atomic():
        briefing.signed_on = signed_on
        briefing.updated_by = by
        briefing.save(update_fields=["signed_on", "updated_by", "updated_at"])
        return [
            attachments.create_attachment(
                target=record, kind=CONFIRMATION_KINDS[kind], file=file, created_by=by
            )
            for file in uploads
        ]


def void_briefing(briefing: Briefing, *, by: User | None, reason: str) -> None:
    """取り消す（支援実績の行を打ち消す。チェック・実施日時・費用・添付は残る）。"""
    if not workers.is_active(briefing.worker):
        raise ValidationError(_("This foreign worker is deactivated."), code="deactivated")
    support_records.void_record(briefing.record, by=by, reason=reason, as_part=True)
    # 生成した確認書の回収のタスクを、共通の規則で戻す（T33）
    from services import form_signatures  # 循環を避ける

    form_signatures.on_briefing_voided(briefing, by=by, reason=reason)


# --- 表示・警告 -------------------------------------------------------------------------


def briefings_of(worker: ForeignWorker) -> models.QuerySet[Briefing]:
    return (
        Briefing.objects.filter(worker=worker)
        .select_related("record__staff", "record__plan_version", "record__voided_by")
        .prefetch_related("sessions", "checks", "fees", "record__languages")
    )


def last_session_on(briefing: Briefing) -> datetime.date | None:
    return max((session.occurred_on for session in briefing.sessions.all()), default=None)


def total_minutes(briefing: Briefing) -> int:
    return sum(session.minutes for session in briefing.sessions.all())


@dataclass(frozen=True)
class ItemState:
    item: ChecklistItem
    check: BriefingCheck | None

    @property
    def status(self) -> str:
        return self.check.status if self.check else ""


@dataclass(frozen=True)
class SectionState:
    section: ChecklistSection
    items: tuple[ItemState, ...]

    @property
    def complete(self) -> bool:
        """欄の完了（配下の細目がすべて「説明した」か「該当なし」）。"""
        return all(state.check is not None for state in self.items)


def checklist_states(briefing: Briefing) -> list[SectionState]:
    checks = {check.code: check for check in briefing.checks.all()}
    return [
        SectionState(
            section=section,
            items=tuple(ItemState(item, checks.get(item.code)) for item in section.items),
        )
        for section in checklist_of(briefing).sections
    ]


@dataclass(frozen=True)
class CheckCounts:
    done: int
    not_applicable: int
    missing: int


def check_counts(briefing: Briefing) -> CheckCounts:
    """細目の状態ごとの数（説明した／該当なし／未チェック）。"""
    statuses = Counter(check.status for check in briefing.checks.all())
    total = len(checklist_of(briefing).item_codes)
    done = statuses[CheckStatus.DONE]
    not_applicable = statuses[CheckStatus.NOT_APPLICABLE]
    return CheckCounts(done, not_applicable, total - done - not_applicable)


def is_signed(briefing: Briefing, record_attachments: Iterable[Attachment] | None = None) -> bool:
    """署名済み: 取り消していない確認書の添付があり、署名日が入っている（一覧と詳細で同じ判定）。

    record_attachments に支援実績の行の添付（一覧で prefetch したもの）を渡すと、それを見る。
    """
    if briefing.signed_on is None:
        return False
    kind = CONFIRMATION_KINDS[SupportItem(briefing.record.kind)]
    files = briefing.record.attachments.all() if record_attachments is None else record_attachments
    return any(file.kind == kind and file.voided_at is None for file in files)


@dataclass(frozen=True)
class BriefingWarning:
    code: str
    message: str
    strong: bool = False  # 基準に適合しないおそれ（色を変える）


def hours_text(minutes: int | Decimal) -> str:
    """分を時間の表示にする（180 → "3"、90 → "1.5"）。"""
    hours = Decimal(minutes) / 60
    return f"{hours.normalize():f}" if hours != hours.to_integral() else f"{int(hours)}"


def planned_minutes(briefing: Briefing) -> int | None:
    """支援計画の実施予定時間（分）。計画・時間が無ければ None。"""
    version = briefing.record.plan_version
    if version is None:
        return None
    item = SupportPlanItem.objects.filter(version=version, item=briefing.record.kind).first()
    if item is None or item.planned_hours is None:
        return None
    return int(item.planned_hours * 60)


def duration_warnings(
    kind: SupportItem, minutes: int, rule: DurationRule, *, continuing: bool
) -> list[BriefingWarning]:
    """所要時間の警告（下限は印にかかわらず。目安は「同一機関で引き続き雇用」の印が無いとき）。"""
    if minutes < rule.minimum_minutes:
        return [
            BriefingWarning(
                "below_minimum",
                _(
                    "Shorter than the minimum of %(hours)s hours, so the %(kind)s cannot be "
                    "regarded as properly given."
                )
                % {"hours": hours_text(rule.minimum_minutes), "kind": kind.label},
                strong=True,
            )
        ]
    if continuing or minutes >= rule.guideline_minutes:
        return []
    hours = hours_text(rule.guideline_minutes)
    if rule.guideline_kind == "at_least":
        message = _("Does not meet the required %(hours)s hours or more.") % {"hours": hours}
    else:
        message = _("Shorter than the guideline of about %(hours)s hours.") % {"hours": hours}
    return [BriefingWarning("below_guideline", message)]


def warnings(briefing: Briefing) -> list[BriefingWarning]:
    """記録の注意（保存は止めない）。取り消した記録には出さない。"""
    if briefing.record.voided_at is not None:
        return []
    kind = SupportItem(briefing.record.kind)
    checklist = checklist_of(briefing)
    result: list[BriefingWarning] = []
    if briefing.deposit_contract == DepositContract.EXISTS:
        result.append(
            BriefingWarning(
                "deposit_contract",
                _(
                    "A contract on a deposit or penalty is declared. This may not meet the "
                    "standards for landing."
                ),
                strong=True,
            )
        )
    if briefing.foreign_fees == ForeignFees.PAID and briefing.fees_agreed is False:
        result.append(
            BriefingWarning(
                "fees_not_agreed",
                _(
                    "The worker has not understood and agreed on the amount and breakdown of the "
                    "fees paid abroad."
                ),
                strong=True,
            )
        )
    missing = check_counts(briefing).missing
    if missing:
        result.append(
            BriefingWarning(
                "missing_items",
                ngettext(
                    "%(count)s required item is not checked.",
                    "%(count)s required items are not checked.",
                    missing,
                )
                % {"count": missing},
            )
        )
    minutes = total_minutes(briefing)
    result.extend(
        duration_warnings(
            kind, minutes, checklist.duration, continuing=briefing.continuing_employment
        )
    )
    planned = planned_minutes(briefing)
    if planned is not None and minutes < planned:
        result.append(
            BriefingWarning(
                "below_plan",
                _("Shorter than the planned %(hours)s hours in the support plan.")
                % {"hours": hours_text(planned)},
            )
        )
    rows = len(briefing.sessions.all())
    if rows > FORM_ROWS:
        result.append(
            BriefingWarning(
                "too_many_rows",
                _(
                    "There are more than %(rows)s dates and times, so they do not fit in the "
                    "rows of the confirmation form."
                )
                % {"rows": FORM_ROWS},
            )
        )
    if not is_signed(briefing):
        result.append(BriefingWarning("unsigned", _("The signed confirmation is not attached.")))
    return result


# --- 集計（T24） ------------------------------------------------------------------------


@dataclass(frozen=True)
class BriefingSummary:
    count: int
    last_on: datetime.date
    minutes: int
    done: int
    not_applicable: int
    missing: int


def summary(
    worker_ids: Iterable[int], date_from: datetime.date, date_to: datetime.date
) -> dict[tuple[int, str], BriefingSummary]:
    """外国人×種別ごとの、期間内（実施日。両端を含む）の件数・最後の実施日・合計時間と、
    細目の状態（説明した／該当なし／未チェック）の数。取り消した記録は数えない。"""
    queryset = (
        Briefing.objects.filter(
            worker_id__in=list(worker_ids),
            record__occurred_on__gte=date_from,
            record__occurred_on__lte=date_to,
            record__voided_at__isnull=True,
        )
        .select_related("record")
        .prefetch_related("sessions", "checks")
    )
    totals: dict[tuple[int, str], list[Any]] = {}
    for briefing in queryset:
        key = (briefing.worker_id, briefing.record.kind)
        counts = check_counts(briefing)
        current = totals.setdefault(key, [0, briefing.record.occurred_on, 0, 0, 0, 0])
        current[0] += 1
        current[1] = max(current[1], briefing.record.occurred_on)
        current[2] += total_minutes(briefing)
        current[3] += counts.done
        current[4] += counts.not_applicable
        current[5] += counts.missing
    return {key: BriefingSummary(*values) for key, values in totals.items()}
