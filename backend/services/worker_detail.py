"""外国人詳細の表示用の組み立て（第5章 F01・F03・F07、T13c）。

- 基本: 最新の在留情報・旅券・住所・連絡先と、在留期限・旅券の有効期限の Deadline。
- 契約: 表示する契約の選び方（有効 → 予定 → 最後に終了した契約）、版、前の版との差分の整形。
- 届出: この外国人の届出タスクを、T14 のダッシュボードと同じ行の形にする。
- 履歴: 在留・旅券・住所・連絡先の履歴行を 1 本の時系列にする（番号は先頭・末尾 2 文字だけ）。
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.contracts.models import (
    DeductionKind,
    EmploymentContract,
    EmploymentContractVersion,
)
from apps.filings.models import FilingStatus
from apps.workers.models import (
    ApplicationType,
    ForeignWorker,
    HistoryRecord,
    InternshipCompletion,
    LatestPassportRecord,
    LatestResidenceRecord,
    LatestWorkerAddress,
    LatestWorkerContact,
    PassportRecord,
    QualificationArea,
    ResidenceRecord,
    WorkerAddress,
    WorkerContact,
    WorkerQualification,
    WorkerRelative,
    WorkHistory,
)
from services import addresses, contracts, dashboard, deadlines, filings, immigration_codes

# --- 基本 -----------------------------------------------------------------------


@dataclass(frozen=True)
class BasicInfo:
    residence: ResidenceRecord | None
    residence_deadline: deadlines.Deadline | None
    passport: PassportRecord | None
    passport_deadline: deadlines.Deadline | None
    address: WorkerAddress | None
    contact: WorkerContact | None
    # 一括申請の欄から逆算して足した行（取り消していないもの。T46a）
    relatives: list[WorkerRelative] = field(default_factory=list)
    qualifications: list[WorkerQualification] = field(default_factory=list)
    internships: list[InternshipCompletion] = field(default_factory=list)
    work_history: list[WorkHistory] = field(default_factory=list)

    def shown_ids(self) -> dict[str, Any]:
        """監査ログに残す、表示した行の ID。"""
        records = {
            "residence_record": self.residence,
            "passport_record": self.passport,
            "address": self.address,
            "contact": self.contact,
        }
        shown: dict[str, Any] = {
            name: record.pk for name, record in records.items() if record is not None
        }
        lists: dict[str, list[Any]] = {
            "relative": self.relatives,
            "qualification": self.qualifications,
            "internship": self.internships,
            "work_history": self.work_history,
        }
        shown.update({name: [row.pk for row in rows] for name, rows in lists.items() if rows})
        return shown

    @property
    def period_of_stay(self) -> str:
        """在留期間（長さ。一括申請ツールの表示名）。"""
        if self.residence is None or not self.residence.period_of_stay:
            return ""
        return immigration_codes.label(
            immigration_codes.PERIOD_OF_STAY, self.residence.period_of_stay
        )

    @property
    def address_split(self) -> str:
        """住所を分けた結果（都道府県・市区町村の名前。分けられなければ空）。"""
        address = self.address
        if address is None or not address.municipality_code:
            return ""
        prefecture = immigration_codes.label(immigration_codes.PREFECTURE, address.prefecture_code)
        municipality = addresses.municipality_name(
            address.prefecture_code, address.municipality_code
        )
        return f"{prefecture} / {municipality} / {address.address_rest}"

    def decrypted_fields(self) -> dict[str, str]:
        """復号して表示した項目（値は残さない）。"""
        shown = {}
        if self.residence is not None and self.residence.card_number:
            shown["card_number"] = "***"
        if self.passport is not None and self.passport.number:
            shown["passport_number"] = "***"
        return shown


def _latest_record(latest_model: type[models.Model], worker: ForeignWorker) -> Any:
    """最新行のビュー（Latest*）から、その行を取る。無ければ None。"""
    latest = latest_model._default_manager.select_related("record").filter(worker=worker).first()
    return getattr(latest, "record", None)


def is_certificate_of_eligibility(record: ResidenceRecord) -> bool:
    """在留資格認定証明書の行（入国前。期限は認定証明書の有効期限。修正 1 回目 B5）。

    認定証明書で入国して在留カードの番号がある行（上陸の許可で交付された在留カード）は含めない。
    """
    return (
        record.application_type == ApplicationType.CERTIFICATE_OF_ELIGIBILITY
        and not record.card_number
    )


def basic_info(worker: ForeignWorker) -> BasicInfo:
    residence: ResidenceRecord | None = _latest_record(LatestResidenceRecord, worker)
    passport: PassportRecord | None = _latest_record(LatestPassportRecord, worker)
    return BasicInfo(
        residence=residence,
        residence_deadline=(
            deadlines.residence_expiry(residence.expires_on)
            if residence is not None and not is_certificate_of_eligibility(residence)
            else None
        ),
        passport=passport,
        passport_deadline=deadlines.passport_expiry(passport.expires_on) if passport else None,
        address=_latest_record(LatestWorkerAddress, worker),
        contact=_latest_record(LatestWorkerContact, worker),
        relatives=list(worker.relatives.filter(voided_at__isnull=True)),
        qualifications=list(
            worker.qualifications.filter(voided_at__isnull=True).prefetch_related("attachments")
        ),
        internships=list(
            worker.internship_completions.filter(voided_at__isnull=True).prefetch_related(
                "attachments"
            )
        ),
        work_history=list(worker.work_history.filter(voided_at__isnull=True)),
    )


# --- 契約 -----------------------------------------------------------------------


def contracts_of(worker: ForeignWorker) -> list[EmploymentContract]:
    """この外国人の全契約（開始日の新しい順）。"""
    return list(
        EmploymentContract.objects.filter(worker=worker)
        .select_related("host_organization")
        .order_by("-current_starts_on", "-pk")
    )


def default_contract(
    all_contracts: Iterable[EmploymentContract], today: datetime.date
) -> EmploymentContract | None:
    """既定で表示する契約: 有効 → 予定（開始の近い順）→ 最後に終了した契約。"""
    by_status: dict[contracts.ContractStatus, list[EmploymentContract]] = {}
    for contract in all_contracts:
        by_status.setdefault(contracts.status_of(contract, today), []).append(contract)
    if active := by_status.get(contracts.ContractStatus.ACTIVE):
        return max(active, key=lambda contract: contract.current_starts_on)
    if scheduled := by_status.get(contracts.ContractStatus.SCHEDULED):
        return min(scheduled, key=lambda contract: contract.current_starts_on)
    if terminated := by_status.get(contracts.ContractStatus.TERMINATED):
        return max(terminated, key=lambda contract: (contract.terminated_on, contract.pk))
    return None


@dataclass(frozen=True)
class DiffRow:
    label: str
    before: str
    after: str


@dataclass(frozen=True)
class ContractView:
    contract: EmploymentContract
    status: contracts.ContractStatus
    versions: list[EmploymentContractVersion]
    version: EmploymentContractVersion
    previous: EmploymentContractVersion | None
    diff: list[DiffRow]
    monthly_total: int | None
    take_home: int | None
    attachments: list[Any] = field(default_factory=list)


def contract_view(
    contract: EmploymentContract, version_number: int | None, today: datetime.date
) -> ContractView:
    """契約の版を 1 つ選んで表示用にまとめる（version_number が無い・不正なら最新の版）。"""
    versions = list(
        contract.versions.select_related("site", "site__field")
        .prefetch_related("categories", "allowances", "deductions", "shifts", "attachments")
        .order_by("-version")
    )
    version = next((v for v in versions if v.version == version_number), versions[0])
    previous = next((v for v in versions if v.version < version.version), None)
    return ContractView(
        contract=contract,
        status=contracts.status_of(contract, today),
        versions=versions,
        version=version,
        previous=previous,
        diff=diff_rows(previous, version) if previous is not None else [],
        monthly_total=contracts.monthly_total(version),
        take_home=contracts.take_home(version),
        attachments=list(version.attachments.all()),
    )


# 円の金額の項目（差分で「¥220,000」の形にする）
YEN_FIELDS = frozenset(
    {"base_wage", "hourly_equivalent", "monthly_equivalent", "comparable_japanese_wage"}
)

# snapshot() の明細の項目名
DETAIL_LABELS = {
    "categories": gettext_lazy("operation categories"),
    "shifts": gettext_lazy("shifts"),
    "allowances": gettext_lazy("allowances"),
    "deductions": gettext_lazy("deductions"),
}


def _format_value(model_field: models.Field[Any, Any] | None, value: Any) -> str:
    if value is None or value == "" or value == []:
        return "—"
    if isinstance(value, bool):
        return _("yes") if value else _("no")
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, datetime.time):
        return value.strftime("%H:%M")
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, int) and model_field is not None and model_field.name in YEN_FIELDS:
        return f"¥{value:,}"
    if model_field is not None and model_field.choices:
        return str(dict(model_field.choices).get(value, value))
    if isinstance(value, list):
        base = model_field.base_field if isinstance(model_field, ArrayField) else None
        return ", ".join(_format_value(base, item) for item in value)
    return str(value)


def _format_detail(name: str, value: list[Any]) -> str:
    if not value:
        return "—"
    if name == "categories":
        return ", ".join(value)
    if name == "shifts":
        return "; ".join(
            f"{start[:5]}–{end[:5]} {applies}".strip() for start, end, applies, _minutes in value
        )
    if name == "allowances":
        return "; ".join(f"{label} ¥{amount:,}" for label, amount, *_rest in value)
    if name == "deductions":
        kinds = dict(DeductionKind.choices)
        return "; ".join(
            f"{name_ or kinds.get(kind, kind)} ¥{amount:,}" for kind, name_, amount in value
        )
    return str(value)


def diff_rows(old: EmploymentContractVersion, new: EmploymentContractVersion) -> list[DiffRow]:
    """T09 の diff() を、項目名（画面の言語）と表示用の値の行にする。"""
    rows = []
    for name, (before, after) in contracts.diff(old, new).items():
        if name in DETAIL_LABELS:
            rows.append(
                DiffRow(
                    str(DETAIL_LABELS[name]),
                    _format_detail(name, before or []),
                    _format_detail(name, after or []),
                )
            )
            continue
        try:
            model_field: models.Field[Any, Any] | None = EmploymentContractVersion._meta.get_field(
                name
            )  # type: ignore[assignment]
        except FieldDoesNotExist:
            model_field = None
        label = str(model_field.verbose_name) if model_field is not None else name
        rows.append(
            DiffRow(label, _format_value(model_field, before), _format_value(model_field, after))
        )
    return rows


# --- 届出 -----------------------------------------------------------------------


def worker_filings(worker: ForeignWorker) -> list[dashboard.FilingRow]:
    """未完了を期限の近い順に先に、完了・取消はその後に新しい順で。"""
    items = list(worker.filings.select_related("assignee"))
    open_items = sorted(
        (f for f in items if FilingStatus(f.status) in filings.OPEN_STATUSES),
        key=lambda f: (f.unadjusted_due_on, f.pk),
    )
    closed = sorted(
        (f for f in items if FilingStatus(f.status) not in filings.OPEN_STATUSES),
        key=lambda f: (f.unadjusted_due_on, f.pk),
        reverse=True,
    )
    return dashboard.filing_rows([*open_items, *closed], timezone.localdate())


# --- 履歴 -----------------------------------------------------------------------


def mask_number(value: str) -> str:
    """過去の番号は先頭 2 文字と末尾 2 文字だけ（全桁は基本タブの最新の 1 件だけに出す）。"""
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return value[:2] + "*" * (len(value) - 4) + value[-2:]


HISTORY_KIND_LABELS = {
    "residence": gettext_lazy("residence"),
    "passport": gettext_lazy("passport"),
    "address": gettext_lazy("residential address"),
    "contact": gettext_lazy("contact"),
    "relative": gettext_lazy("relative or housemate in Japan"),
    "qualification": gettext_lazy("skill or Japanese language qualification"),
    "internship": gettext_lazy("completed Technical Intern Training (ii)"),
    "work": gettext_lazy("work history"),
}


@dataclass(frozen=True)
class HistoryEntry:
    kind: str  # residence / passport / address / contact
    record: HistoryRecord
    on: datetime.date  # 許可日・発行日・適用開始日
    masked_number: str  # 在留カード番号・旅券番号（先頭・末尾 2 文字だけ）
    details: list[tuple[str, str]]

    @property
    def kind_label(self) -> str:
        return str(HISTORY_KIND_LABELS[self.kind])

    @property
    def voided(self) -> bool:
        return self.record.voided_at is not None


def _label(model: type[models.Model], name: str) -> str:
    return str(model._meta.get_field(name).verbose_name)  # type: ignore[union-attr]


def history(worker: ForeignWorker) -> list[HistoryEntry]:
    """在留・旅券・住所・連絡先の履歴（日付の新しい順。同じ日は登録の新しい順）。"""
    entries: list[HistoryEntry] = []
    for residence in worker.residence_records.select_related("voided_by"):
        entries.append(
            HistoryEntry(
                "residence",
                residence,
                residence.permitted_on,
                mask_number(residence.card_number),
                [
                    (
                        _label(ResidenceRecord, "status_of_residence"),
                        residence.get_status_of_residence_display(),
                    ),
                    (_label(ResidenceRecord, "expires_on"), residence.expires_on.isoformat()),
                    (
                        _label(ResidenceRecord, "application_type"),
                        residence.get_application_type_display(),
                    ),
                ],
            )
        )
    for passport in worker.passport_records.select_related("issuing_country", "voided_by"):
        entries.append(
            HistoryEntry(
                "passport",
                passport,
                passport.issued_on,
                mask_number(passport.number),
                [
                    (_label(PassportRecord, "issuing_country"), passport.issuing_country.name),
                    (_label(PassportRecord, "expires_on"), passport.expires_on.isoformat()),
                ],
            )
        )
    for address in worker.addresses.select_related("voided_by"):
        entries.append(
            HistoryEntry(
                "address",
                address,
                address.effective_from,
                "",
                [(_label(WorkerAddress, "address"), f"〒{address.postal_code} {address.address}")],
            )
        )
    for contact in worker.contacts.select_related("voided_by"):
        details = [
            (_label(WorkerContact, name), str(getattr(contact, name)))
            for name in ("phone", "email", "emergency_name", "emergency_phone")
            if getattr(contact, name)
        ]
        entries.append(HistoryEntry("contact", contact, contact.effective_on, "", details))
    entries.extend(_application_entries(worker))
    entries.sort(key=lambda entry: (entry.on, entry.record.created_at), reverse=True)
    return entries


def _registered_on(record: HistoryRecord) -> datetime.date:
    return timezone.localdate(record.created_at)


def relative_details(relative: WorkerRelative) -> list[tuple[str, str]]:
    details = [
        (
            _label(WorkerRelative, "relationship"),
            immigration_codes.label(immigration_codes.RELATIONSHIP, relative.relationship),
        ),
        (_label(WorkerRelative, "name"), relative.name),
        (
            _label(WorkerRelative, "nationality_code"),
            immigration_codes.label(immigration_codes.COUNTRY, relative.nationality_code),
        ),
        (
            _label(WorkerRelative, "lives_together"),
            _("yes") if relative.lives_together else _("no"),
        ),
    ]
    if relative.birth_date:
        details.append((_label(WorkerRelative, "birth_date"), relative.birth_date.isoformat()))
    if relative.workplace:
        details.append((_label(WorkerRelative, "workplace"), relative.workplace))
    return details


def qualification_details(row: WorkerQualification) -> list[tuple[str, str]]:
    exam_code = (
        immigration_codes.SKILL_EXAM
        if row.area == QualificationArea.SKILL
        else immigration_codes.JAPANESE_EXAM
    )
    details = [
        (_label(WorkerQualification, "area"), row.get_area_display()),
        (
            _label(WorkerQualification, "evaluation"),
            immigration_codes.label(immigration_codes.EVALUATION, row.evaluation),
        ),
    ]
    if row.exam:
        details.append(
            (_label(WorkerQualification, "exam"), immigration_codes.label(exam_code, row.exam))
        )
    if row.exam_country:
        details.append(
            (
                _label(WorkerQualification, "exam_country"),
                immigration_codes.label(immigration_codes.COUNTRY, row.exam_country),
            )
        )
    for name in ("passed_on", "level", "other_proof"):
        value = getattr(row, name)
        if value:
            details.append((_label(WorkerQualification, name), str(value)))
    return details


def internship_details(row: InternshipCompletion) -> list[tuple[str, str]]:
    details = [
        (
            _label(InternshipCompletion, "occupation"),
            immigration_codes.label(immigration_codes.INTERN_OCCUPATION, row.occupation),
        ),
        (
            _label(InternshipCompletion, "work"),
            immigration_codes.linked_label(
                immigration_codes.INTERN_OCCUPATION, row.occupation, row.work
            ),
        ),
        (
            _label(InternshipCompletion, "proof"),
            immigration_codes.label(immigration_codes.INTERN_PROOF, row.proof),
        ),
    ]
    if row.completed_on:
        details.append((_label(InternshipCompletion, "completed_on"), str(row.completed_on)))
    return details


def work_details(row: WorkHistory) -> list[tuple[str, str]]:
    period = (
        f"{row.joined_on:%Y-%m}〜{row.left_on:%Y-%m}" if row.left_on else f"{row.joined_on:%Y-%m}〜"
    )
    return [(_label(WorkHistory, "employer"), row.employer), (_("period"), period)]


def _application_entries(worker: ForeignWorker) -> list[HistoryEntry]:
    """在日親族・試験・技能実習・職歴の行（T46a）。"""
    entries = [
        HistoryEntry(
            "relative",
            relative,
            _registered_on(relative),
            mask_number(relative.card_number),
            relative_details(relative),
        )
        for relative in worker.relatives.select_related("voided_by")
    ]
    entries += [
        HistoryEntry(
            "qualification",
            row,
            row.passed_on or _registered_on(row),
            "",
            qualification_details(row),
        )
        for row in worker.qualifications.select_related("voided_by")
    ]
    entries += [
        HistoryEntry(
            "internship", row, row.completed_on or _registered_on(row), "", internship_details(row)
        )
        for row in worker.internship_completions.select_related("voided_by")
    ]
    entries += [
        HistoryEntry("work", row, row.joined_on, "", work_details(row))
        for row in worker.work_history.select_related("voided_by")
    ]
    return entries


@dataclass(frozen=True)
class ApplicationRow:
    record: HistoryRecord
    masked_number: str
    details: list[tuple[str, str]]
    attachments: list[Any]


@dataclass(frozen=True)
class ApplicationSection:
    """基本タブの「一括申請の項目」の 1 つの区分（T46a）。"""

    kind: str  # 履歴の区分（URL の <kind>）
    rows: list[ApplicationRow]

    @property
    def title(self) -> str:
        return str(HISTORY_KIND_LABELS[self.kind])


def application_sections(info: BasicInfo) -> list[ApplicationSection]:
    def attached(row: Any) -> list[Any]:
        return [a for a in row.attachments.all() if a.voided_at is None]

    return [
        ApplicationSection(
            "relative",
            [
                ApplicationRow(row, mask_number(row.card_number), relative_details(row), [])
                for row in info.relatives
            ],
        ),
        ApplicationSection(
            "qualification",
            [
                ApplicationRow(row, "", qualification_details(row), attached(row))
                for row in info.qualifications
            ],
        ),
        ApplicationSection(
            "internship",
            [
                ApplicationRow(row, "", internship_details(row), attached(row))
                for row in info.internships
            ],
        ),
        ApplicationSection(
            "work", [ApplicationRow(row, "", work_details(row), []) for row in info.work_history]
        ),
    ]
