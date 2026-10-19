"""申請の書類の一覧（「特定技能１号」に係る提出書類一覧・確認表。T37、ADR-0026）。

定義は application_sets/<申請の種類>/<適用開始日>.json（tools/forms/make_application_sets.py が
入管の Excel から作る。第1〜3表の全部の行。T37-D2）。外国人×申請の種類×基準日で、行ごとに次を出す。
- 当てはまるか（法人・個人、分野、支援の全部の委託、1 年単位の変形労働時間制、国籍）。
  当てはまらない行も一覧の下に出す。適正な受入れ実績のある機関の行（第2表の1）は、システムでは
  判断しない。
- どう満たすか（生成する様式 → 生成の有無・署名の状態 ／ 添付 → 添付の有無・有効期限 ／
  システムの外）。
健康診断の書類（要配慮。ADR-0011）は、有無と日付だけを出す（ファイル名もリンクも出さない）。
"""

from __future__ import annotations

import datetime
import enum
import json
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from django.conf import settings
from django.urls import reverse

from apps.contracts.models import EmploymentContract, EmploymentContractVersion, VariableHoursUnit
from apps.core.models import Attachment, AttachmentKind, Organization
from apps.forms_engine.models import GeneratedDocument, GenerationStatus
from apps.hosts.models import EntityType, SupportScope
from apps.support.models import SupportPlan
from apps.workers.models import (
    ApplicationType,
    ForeignWorker,
    LatestResidenceRecord,
    StatusOfResidence,
)
from services import contracts, deadlines, form_languages, form_signatures, hosts, support_plans

SET_IDS = ("coe", "chg", "ext")
INDUSTRIAL = "industrial_products"
FOOD = "food_manufacturing"
# 雇用契約を対象に生成する様式（契約タブ・申請の書類の一覧から生成する）
CONTRACT_VERSION_FORMS = ("1-4", "1-5", "1-6")
CONTRACT_FORMS = ("3-1", "13-1", "13-2", "5-10", "1-25")


class Status(enum.StrEnum):
    GENERATED = "generated"  # 生成済み（基準日の契約・版を対象にしたもの）
    OTHER_TARGET = "other_target"  # 生成済みだが、ほかの契約・版を対象にしたもの
    NOT_GENERATED = "not_generated"
    ATTACHED = "attached"
    EXPIRED = "expired"  # 加入の有効期限を過ぎた（無いものとみなす。ADR-0004）
    MISSING = "missing"
    OUTSIDE = "outside"  # システムの外で用意する
    ALTERNATIVE = "alternative"  # 実績のある機関の代わりの書類（システムでは判断しない）
    UNKNOWN = "unknown"  # 基準日の雇用契約が無いので判断できない
    # 健康診断の受診が申請に使える時期の外・受診日が未登録（警告。修正 1 回目 B8）
    OUT_OF_PERIOD = "out_of_period"
    DATE_MISSING = "date_missing"
    GENERATING = "generating"  # 生成を依頼した後、ジョブが終わる前（修正 1 回目 B10）


# 行の色とラベルの区分（修正 1 回目 B9）: 不足＝赤、確かめが要る＝黄、済み＝緑、
# システムの外・判断しない＝灰
TONES: dict[Status, str] = {
    Status.NOT_GENERATED: "danger",
    Status.MISSING: "danger",
    Status.EXPIRED: "danger",
    Status.OTHER_TARGET: "warning",
    Status.OUT_OF_PERIOD: "warning",
    Status.DATE_MISSING: "warning",
    Status.GENERATED: "ok",
    Status.ATTACHED: "ok",
    Status.GENERATING: "muted",
    Status.OUTSIDE: "muted",
    Status.ALTERNATIVE: "muted",
    Status.UNKNOWN: "muted",
}


DONE = frozenset({Status.GENERATED, Status.ATTACHED, Status.OUTSIDE, Status.ALTERNATIVE})


class DefinitionError(Exception):
    pass


@dataclass(frozen=True)
class Definition:
    id: str
    name: str
    valid_from: datetime.date
    rows: tuple[dict[str, Any], ...]
    path: Path


def _root() -> Path:
    return Path(settings.REPO_ROOT) / "application_sets"


@cache
def _read(path: Path) -> Definition:
    data = json.loads(path.read_text(encoding="utf-8"))
    return Definition(
        id=data["id"],
        name=data["name"],
        valid_from=datetime.date.fromisoformat(data["valid_from"]),
        rows=tuple(data["rows"]),
        path=path,
    )


def definition(set_id: str, on: datetime.date) -> Definition:
    """基準日に適用される一覧（無ければ最も古いもの）。"""
    if set_id not in SET_IDS:
        raise DefinitionError(set_id)
    files = sorted((_root() / set_id).glob("*.json"))
    if not files:
        raise DefinitionError(set_id)
    found = [_read(path) for path in files]
    applied = [item for item in found if item.valid_from <= on]
    return applied[-1] if applied else found[0]


def default_set(worker: ForeignWorker) -> str:
    """申請の種類の初期値（T37-D3）。

    在留情報が無い・認定証明書 → 認定、特定技能1号 → 更新、ほか → 変更。
    """
    latest = LatestResidenceRecord.objects.select_related("record").filter(worker=worker).first()
    if latest is None:
        return "coe"
    record = latest.record
    if record.application_type == ApplicationType.CERTIFICATE_OF_ELIGIBILITY:
        return "coe"
    if record.status_of_residence == StatusOfResidence.SSW_1:
        return "ext"
    return "chg"


@dataclass
class Facts:
    """行の当てはまりと状態の判断に使う事実（基準日）。"""

    worker: ForeignWorker
    on: datetime.date
    contract: EmploymentContract | None
    version: EmploymentContractVersion | None
    full_support: bool | None
    organization: Organization | None
    documents: dict[str, GeneratedDocument] = field(default_factory=dict)
    requested: dict[str, GeneratedDocument] = field(default_factory=dict)  # 生成中（最新の依頼）
    # 自作の訳文の版の生成物（様式 ID ごとの新しい順。生成の有無には数えない。T43）
    translations: dict[str, list[GeneratedDocument]] = field(default_factory=dict)
    set_id: str = ""  # 申請の種類（健康診断の受診の時期が申請の種類で違う）


def facts_for(worker: ForeignWorker, on: datetime.date) -> Facts:
    contract = contracts.contract_on(worker, on)
    version = contracts.version_on(contract, on) if contract else None
    full_support = None
    if contract is not None:
        support = hosts.active_contract(contract.host_organization, on)
        full_support = support is not None and support.scope == SupportScope.FULL
    documents: dict[str, GeneratedDocument] = {}
    translations: dict[str, list[GeneratedDocument]] = {}
    queryset = (
        GeneratedDocument.objects.filter(worker=worker, status=GenerationStatus.COMPLETED)
        .select_related("template", "content_type")
        .order_by("-generated_at", "-pk")
    )
    for document in queryset:
        if document.template.language in form_languages.TRANSLATIONS:
            translations.setdefault(document.template.form_id, []).append(document)
            continue
        documents.setdefault(document.template.form_id, document)
    requested: dict[str, GeneratedDocument] = {}
    for document in (
        GeneratedDocument.objects.filter(worker=worker, status=GenerationStatus.REQUESTED)
        .exclude(template__language__in=form_languages.TRANSLATIONS)
        .select_related("template")
        .order_by("-created_at", "-pk")
    ):
        requested.setdefault(document.template.form_id, document)
    return Facts(
        worker=worker,
        on=on,
        contract=contract,
        version=version,
        full_support=full_support,
        organization=Organization.objects.first(),
        documents=documents,
        requested=requested,
        translations=translations,
    )


def applies(row: dict[str, Any], facts: Facts) -> bool | None:
    """行が当てはまるか（None は判断できない）。"""
    result: bool | None = True
    for condition in row["applies"]:
        value = _condition(condition, facts)
        if value is False:
            return False
        if value is None:
            result = None
    return result


def _condition(condition: str, facts: Facts) -> bool | None:
    if condition == "track_record":
        return None
    if condition.startswith("nationality:"):
        codes = condition.split(":", 1)[1].split(",")
        return facts.worker.nationality.code in codes
    contract, version = facts.contract, facts.version
    if contract is None or version is None:
        return None
    if condition == "corporate":
        return contract.host_organization.entity_type == EntityType.CORPORATION
    if condition == "individual":
        return contract.host_organization.entity_type == EntityType.SOLE_PROPRIETOR
    if condition == "industrial":
        return version.site.field.code == INDUSTRIAL
    if condition == "food":
        return version.site.field.code == FOOD
    if condition == "full_support":
        return facts.full_support
    if condition == "not_full_support":
        return None if facts.full_support is None else not facts.full_support
    if condition == "variable_one_year":
        return version.variable_hours_unit == VariableHoursUnit.ONE_YEAR
    raise DefinitionError(condition)


@dataclass(frozen=True)
class RowState:
    row: dict[str, Any]
    applicable: bool | None
    status: Status
    detail: str = ""  # 日付・署名の状態など（ファイル名は出さない）
    action_url: str = ""  # 生成・添付の画面
    document: GeneratedDocument | None = None
    signature: str = ""  # form_signatures.SignatureState の値
    attach_url: str = ""  # 添付の画面（モーダルで開く。健康診断の書類。修正 1 回目 B8）

    @property
    def done(self) -> bool:
        return self.status in DONE

    @property
    def tone(self) -> str:
        return TONES[self.status]


def _generate_url(form_id: str, facts: Facts) -> str:
    worker, contract, version = facts.worker, facts.contract, facts.version
    if form_id == "1-17":
        plan = SupportPlan.objects.filter(worker=worker).order_by("-pk").first()
        plan_version = (
            (support_plans.current_version(plan, facts.on) or support_plans.latest_version(plan))
            if plan
            else None
        )
        if plan is None or plan_version is None:
            return reverse("workers:support", args=[worker.pk])
        return reverse(
            "workers:support_plan_generate", args=[worker.pk, plan.pk, plan_version.version]
        )
    if contract is None or version is None:
        return reverse("workers:contracts", args=[worker.pk])
    if form_id in CONTRACT_VERSION_FORMS:
        return reverse(
            "workers:contract_generate", args=[worker.pk, contract.pk, version.version, form_id]
        )
    return reverse("workers:contract_own_document", args=[worker.pk, contract.pk, form_id])


def _expected_target(form_id: str, facts: Facts) -> Any:
    if form_id in CONTRACT_VERSION_FORMS:
        return facts.version
    if form_id in CONTRACT_FORMS:
        return facts.contract
    return None  # 支援計画書は計画の版ごと（どの版でも生成済みとする）


def _generated(row: dict[str, Any], applicable: bool | None, facts: Facts) -> RowState:
    form_id = row["how"]["generate"]
    url = _generate_url(form_id, facts)
    document = facts.documents.get(form_id)
    pending = facts.requested.get(form_id)
    if pending is not None and (document is None or pending.created_at > document.created_at):
        return RowState(row, applicable, Status.GENERATING, action_url=url, document=pending)
    if document is None:
        return RowState(row, applicable, Status.NOT_GENERATED, action_url=url)
    expected = _expected_target(form_id, facts)
    signature = form_signatures.state(document)
    detail = document.generated_at.date().isoformat() if document.generated_at else ""
    same = expected is None or (
        document.content_type.model_class() is type(expected) and document.object_id == expected.pk
    )
    status = Status.GENERATED if same else Status.OTHER_TARGET
    return RowState(
        row, applicable, status, detail, url, document=document, signature=str(signature)
    )


# 健康診断の受診が申請に使える時期（運用要領 本体 印刷16頁【留意事項】。修正 1 回目 B8）:
# 認定は申請の日から遡って 3 か月以内、変更は 1 年以内（日本の医療機関。システムでは確かめない）。
# 更新は健康診断が要らない
HEALTH_CHECK_MONTHS = {"coe": 3, "chg": 12}


def health_check_earliest(set_id: str, on: datetime.date) -> datetime.date | None:
    months = HEALTH_CHECK_MONTHS.get(set_id)
    return deadlines.add_months(on, -months) if months else None


def _attachment_state(row: dict[str, Any], applicable: bool | None, facts: Facts) -> RowState:
    kind = row["how"]["attachment"]
    worker, contract, version = facts.worker, facts.contract, facts.version
    if kind == "health_check":
        wanted = (
            AttachmentKind.HEALTH_DECLARATION
            if "申告書" in row["document"]
            else AttachmentKind.HEALTH_CHECK_RECORD
        )
        latest = (
            Attachment.objects.filter(
                content_type__app_label="workers",
                content_type__model="foreignworker",
                object_id=worker.pk,
                kind=wanted,
                voided_at__isnull=True,
            )
            .order_by("-created_at")
            .first()
        )
        url = reverse("workers:detail", args=[worker.pk]) + "#health-documents"
        attach = reverse("workers:health_document_add", args=[worker.pk]) + f"?kind={wanted.value}"
        if latest is None:
            return RowState(row, applicable, Status.MISSING, action_url=url, attach_url=attach)
        if latest.examined_on is None:
            return RowState(row, applicable, Status.DATE_MISSING, action_url=url, attach_url=attach)
        detail = latest.examined_on.isoformat()
        earliest = health_check_earliest(facts.set_id, facts.on)
        status = (
            Status.OUT_OF_PERIOD
            if earliest is not None and latest.examined_on < earliest
            else Status.ATTACHED
        )
        return RowState(row, applicable, status, detail, url, attach_url=attach)
    if kind == "own_council_certificate":
        organization = facts.organization
        url = reverse("settings:organization_edit")
        attached = (
            organization is not None
            and organization.food_council_joined_on is not None
            and organization.attachments.filter(
                kind=AttachmentKind.FOOD_COUNCIL_CERTIFICATE, voided_at__isnull=True
            ).exists()
        )
        if not attached:
            return RowState(row, applicable, Status.MISSING, action_url=url)
        assert organization is not None and organization.food_council_joined_on is not None
        return RowState(
            row, applicable, Status.ATTACHED, organization.food_council_joined_on.isoformat(), url
        )
    if contract is None or version is None:
        return RowState(row, applicable, Status.UNKNOWN)
    if kind == "membership_certificate":
        host = contract.host_organization
        url = reverse("hosts:detail", args=[host.pk])
        membership = host.memberships.filter(field=version.site.field).first()
        if membership is None or membership.joined_on > facts.on:
            return RowState(row, applicable, Status.MISSING, action_url=url)
        if membership.valid_until is not None and membership.valid_until < facts.on:
            return RowState(
                row, applicable, Status.EXPIRED, membership.valid_until.isoformat(), url
            )
        if not membership.attachments.filter(
            kind=AttachmentKind.MEMBERSHIP_CERTIFICATE, voided_at__isnull=True
        ).exists():
            return RowState(row, applicable, Status.MISSING, action_url=url)
        until = membership.valid_until.isoformat() if membership.valid_until else ""
        return RowState(row, applicable, Status.ATTACHED, until, url)
    # 版の根拠資料（同等報酬・変形労働時間制）
    url = reverse("workers:contracts", args=[worker.pk]) + f"?contract={contract.pk}"
    exists = version.attachments.filter(kind=kind, voided_at__isnull=True).exists()
    return RowState(row, applicable, Status.ATTACHED if exists else Status.MISSING, action_url=url)


def row_attachments(row: dict[str, Any], facts: Facts) -> list[Attachment]:
    """添付の行を満たす添付（取り消していないもの。統合 PDF に綴じる。T42）。

    健康診断の書類・構成員の証明書・協議会の証明書は最新の 1 件、版の根拠資料は全部（古い順）。
    当てはまるかの判断は state_of と同じ（有効期限を過ぎた加入の証明書は綴じない）。
    """
    kind = row["how"].get("attachment")
    worker, contract, version = facts.worker, facts.contract, facts.version
    live = Attachment.objects.filter(voided_at__isnull=True)
    if kind == "health_check":
        wanted = (
            AttachmentKind.HEALTH_DECLARATION
            if "申告書" in row["document"]
            else AttachmentKind.HEALTH_CHECK_RECORD
        )
        latest = (
            live.filter(
                content_type__app_label="workers",
                content_type__model="foreignworker",
                object_id=worker.pk,
                kind=wanted,
            )
            .order_by("-created_at")
            .first()
        )
        return [latest] if latest else []
    if kind == "own_council_certificate":
        organization = facts.organization
        if organization is None or organization.food_council_joined_on is None:
            return []
        latest = (
            organization.attachments.filter(
                kind=AttachmentKind.FOOD_COUNCIL_CERTIFICATE, voided_at__isnull=True
            )
            .order_by("-created_at")
            .first()
        )
        return [latest] if latest else []
    if contract is None or version is None:
        return []
    if kind == "membership_certificate":
        membership = contract.host_organization.memberships.filter(field=version.site.field).first()
        if membership is None or membership.joined_on > facts.on:
            return []
        if membership.valid_until is not None and membership.valid_until < facts.on:
            return []
        latest = (
            membership.attachments.filter(
                kind=AttachmentKind.MEMBERSHIP_CERTIFICATE, voided_at__isnull=True
            )
            .order_by("-created_at")
            .first()
        )
        return [latest] if latest else []
    return list(
        version.attachments.filter(kind=kind, voided_at__isnull=True).order_by("created_at", "pk")
    )


def translation_of(document: GeneratedDocument, facts: Facts) -> GeneratedDocument | None:
    """日本語の版の生成物と同じ対象の、最新の訳文の版の生成物（T43。統合 PDF で直後に綴じる）。"""
    for translation in facts.translations.get(document.template.form_id, []):
        same = (
            translation.content_type_id == document.content_type_id
            and translation.object_id == document.object_id
        )
        if same:
            return translation
    return None


def timezone_date(value: datetime.datetime) -> str:
    from django.utils import timezone

    return timezone.localtime(value).date().isoformat()


def state_of(row: dict[str, Any], facts: Facts) -> RowState:
    applicable = applies(row, facts)
    how = row["how"]
    if "track_record" in row["applies"]:
        return RowState(row, applicable, Status.ALTERNATIVE)
    if "out" in how:
        return RowState(row, applicable, Status.OUTSIDE)
    if "generate" in how:
        if facts.contract is None and how["generate"] != "1-17":
            return RowState(row, applicable, Status.UNKNOWN, action_url=_generate_url("", facts))
        return _generated(row, applicable, facts)
    return _attachment_state(row, applicable, facts)


@dataclass(frozen=True)
class Overview:
    definition: Definition
    facts: Facts
    rows: list[RowState]  # 当てはまる・判断できない行（表の順）
    not_applicable: list[RowState]

    @property
    def open_count(self) -> int:
        return sum(1 for row in self.rows if not row.done and row.row["need"] == "○")

    @property
    def shown_documents(self) -> list[int]:
        return [row.document.pk for row in self.rows if row.document is not None]


def overview(worker: ForeignWorker, set_id: str, on: datetime.date) -> Overview:
    found = definition(set_id, on)
    facts = facts_for(worker, on)
    facts.set_id = set_id
    states = [state_of(row, facts) for row in found.rows]
    return Overview(
        definition=found,
        facts=facts,
        rows=[state for state in states if state.applicable is not False],
        not_applicable=[state for state in states if state.applicable is False],
    )
