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
    LatestPassportRecord,
    LatestResidenceRecord,
    LatestWorkerAddress,
    LatestWorkerContact,
    PassportRecord,
    ResidenceRecord,
    WorkerAddress,
    WorkerContact,
)
from services import contracts, dashboard, deadlines, filings

# --- 基本 -----------------------------------------------------------------------


@dataclass(frozen=True)
class BasicInfo:
    residence: ResidenceRecord | None
    residence_deadline: deadlines.Deadline | None
    passport: PassportRecord | None
    passport_deadline: deadlines.Deadline | None
    address: WorkerAddress | None
    contact: WorkerContact | None

    def shown_ids(self) -> dict[str, int]:
        """監査ログに残す、表示した行の ID。"""
        records = {
            "residence_record": self.residence,
            "passport_record": self.passport,
            "address": self.address,
            "contact": self.contact,
        }
        return {name: record.pk for name, record in records.items() if record is not None}

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
    entries.sort(key=lambda entry: (entry.on, entry.record.created_at), reverse=True)
    return entries
