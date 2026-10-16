"""雇用契約の業務ロジック（第5章 F03・F07、第7章 employment_contracts）。

- 版の追加: 新しい版を作り、見出しの現在の期間を同じトランザクションで更新する。
- 状態（予定／有効／終了）は列で持たず、日付と終了の登録から決める。
- 期間の重なり・派遣の不可（事業所は契約相手の受入れ機関のもの）・業務区分は保存前に検査する
  （DB の排他制約もあるが、admin のインラインでは制約の検査が飛ばされるため事前に見る）。
- 版どうしの差分（diff）は T11 の随時届出の根拠になる。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Any

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Max, Q
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.contracts.models import (
    EmploymentContract,
    EmploymentContractVersion,
    TerminationReason,
    WageType,
)
from apps.core.models import AttachmentKind, User
from apps.hosts.models import HostOrganization, HostSite, OperationCategory
from apps.workers.models import ForeignWorker


class ContractStatus(models.TextChoices):
    SCHEDULED = "scheduled", gettext_lazy("scheduled")
    ACTIVE = "active", gettext_lazy("active")
    TERMINATED = "terminated", gettext_lazy("terminated")


def status_of(contract: EmploymentContract, on: date) -> ContractStatus:
    if contract.terminated_on is not None:
        return ContractStatus.TERMINATED
    if on < contract.current_starts_on:
        return ContractStatus.SCHEDULED
    return ContractStatus.ACTIVE


def status_q(status: ContractStatus, on: date) -> Q:
    """一覧の絞り込み用（status_of と同じ判定）。"""
    if status is ContractStatus.TERMINATED:
        return Q(terminated_on__isnull=False)
    if status is ContractStatus.SCHEDULED:
        return Q(terminated_on__isnull=True, current_starts_on__gt=on)
    return Q(terminated_on__isnull=True, current_starts_on__lte=on)


def latest_version(contract: EmploymentContract) -> EmploymentContractVersion | None:
    return contract.versions.order_by("-version").first()


# --- 検査 ----------------------------------------------------------------------


def _effective_end(ends_on: date | None, terminated_on: date | None) -> date | None:
    ends = [d for d in (ends_on, terminated_on) if d is not None]
    return min(ends) if ends else None


def validate_no_overlap(
    worker_id: int | None,
    starts_on: date | None,
    ends_on: date | None,
    terminated_on: date | None = None,
    *,
    exclude_contract_id: int | None = None,
) -> None:
    """同じ外国人の契約と期間（開始日〜版の終了日と終了の登録日の早いほう）が重ならないこと。"""
    if worker_id is None or starts_on is None:
        return
    end = _effective_end(ends_on, terminated_on)
    others = EmploymentContract.objects.filter(worker_id=worker_id)
    if exclude_contract_id is not None:
        others = others.exclude(pk=exclude_contract_id)
    for other in others:
        other_end = _effective_end(other.current_ends_on, other.terminated_on)
        if (end is None or end >= other.current_starts_on) and (
            other_end is None or other_end >= starts_on
        ):
            raise ValidationError(
                _(
                    "This period overlaps the employment contract (ID: %(id)s) "
                    "of the same foreign worker."
                )
                % {"id": other.pk},
                code="overlap",
            )


def validate_site_belongs_to_host(host: HostOrganization | None, site: HostSite | None) -> None:
    """派遣は不可。就業場所は契約相手（受入れ機関）の事業所に限る。"""
    if host is not None and site is not None and site.host_organization_id != host.pk:
        raise ValidationError(
            _("The workplace must be a site of the contracting host organization (no dispatch)."),
            code="dispatch",
        )


def validate_categories_of_site(
    site: HostSite | None, categories: Iterable[OperationCategory]
) -> None:
    """業務区分は事業所の業務区分の中から選ぶ。"""
    if site is None:
        return
    allowed = set(site.categories.values_list("pk", flat=True))
    others = [c for c in categories if c.pk not in allowed]
    if others:
        raise ValidationError(
            _("These operation categories are not registered for the site: %(names)s")
            % {"names": ", ".join(str(c) for c in others)},
            code="category_site",
        )


def validate_change(contract: EmploymentContract | None, change_reason: str) -> None:
    """2 版目以降は変更理由が必須。"""
    if contract is not None and contract.versions.exists() and not change_reason.strip():
        raise ValidationError(_("Enter the reason for the change."), code="change_reason")


def validate_termination(
    terminated_on: date | None, reason: str, note: str, starts_on: date | None
) -> None:
    if terminated_on is None:
        return
    if not reason:
        raise ValidationError(_("Select the termination reason."), code="termination_reason")
    if reason == TerminationReason.OTHER and not note.strip():
        raise ValidationError(
            _("Enter a note for the termination reason."), code="termination_note"
        )
    if starts_on is not None and terminated_on < starts_on:
        raise ValidationError(
            _("The termination date must be on or after the start date."), code="termination_date"
        )


# --- 版の追加・終了 ------------------------------------------------------------------


@transaction.atomic
def add_version(
    version: EmploymentContractVersion,
    *,
    contract: EmploymentContract | None,
    worker: ForeignWorker | None = None,
    host_organization: HostOrganization | None = None,
    by: User | None,
) -> EmploymentContractVersion:
    """新しい版を保存する。contract が None なら見出しも作る（1 版目）。

    業務区分（多対多）と明細行は、呼び出し側が版の保存後に保存する。
    """
    if contract is None:
        if worker is None or host_organization is None:
            raise ValueError("worker and host_organization are required for a new contract")
        contract = EmploymentContract(
            worker=worker,
            host_organization=host_organization,
            created_by=by,
        )
    contract.current_starts_on = version.starts_on
    contract.current_ends_on = version.ends_on
    contract.updated_by = by
    contract.save()

    number = (contract.versions.aggregate(n=Max("version"))["n"] or 0) + 1
    version.contract = contract
    version.version = number
    if number == 1 and version.changed_on is None:
        version.changed_on = version.starts_on
    version.created_by = by
    version.updated_by = by
    version.save()
    return version


def terminate(
    contract: EmploymentContract,
    *,
    on: date,
    reason: str,
    note: str = "",
    by: User | None,
) -> None:
    """終了を登録する。監査ログには見出しの更新（終了日・理由の前後）として 1 件残る。"""
    validate_termination(on, reason, note, contract.current_starts_on)
    contract.terminated_on = on
    contract.termination_reason = reason
    contract.termination_note = note
    contract.updated_by = by
    contract.save(
        update_fields=[
            "terminated_on",
            "termination_reason",
            "termination_note",
            "updated_by",
            "updated_at",
        ]
    )
    # T11: ここで「終了から 14 日以内の届出」を自動起票する


def cancel_termination(contract: EmploymentContract, *, by: User | None) -> None:
    """終了の登録を取り消す（入力ミスの訂正。管理者のみ。期間の重なりを確かめる）。"""
    validate_no_overlap(
        contract.worker_id,
        contract.current_starts_on,
        contract.current_ends_on,
        None,
        exclude_contract_id=contract.pk,
    )
    contract.terminated_on = None
    contract.termination_reason = ""
    contract.termination_note = ""
    contract.updated_by = by
    contract.save(
        update_fields=[
            "terminated_on",
            "termination_reason",
            "termination_note",
            "updated_by",
            "updated_at",
        ]
    )


# --- 差分・金額 ------------------------------------------------------------------

# 差分に含めない列（版の識別・変更の記録そのもの・作成者等）
_META_FIELDS = frozenset(
    {
        "id",
        "contract",
        "version",
        "change_reason",
        "changed_on",
        "signed_on",
        "created_at",
        "updated_at",
        "created_by",
        "updated_by",
    }
)


def snapshot(version: EmploymentContractVersion) -> dict[str, Any]:
    """差分の比較に使う値（事業所・業務区分は名称、明細は行の一覧）。"""
    values: dict[str, Any] = {}
    for field in version._meta.concrete_fields:
        if field.name in _META_FIELDS:
            continue
        value = field.value_from_object(version)
        if field.name == "site":
            value = str(version.site)
        elif isinstance(value, list):
            value = sorted(value)
        values[field.name] = value
    values["categories"] = sorted(str(c) for c in version.categories.all())
    values["shifts"] = [
        (s.start_time.isoformat(), s.end_time.isoformat(), s.applies_to, s.daily_working_minutes)
        for s in version.shifts.order_by("start_time", "pk")
    ]
    values["allowances"] = [
        (a.name, a.amount, a.calculation_method, a.fixed_overtime_hours)
        for a in version.allowances.order_by("pk")
    ]
    values["deductions"] = [(d.kind, d.name, d.amount) for d in version.deductions.order_by("pk")]
    return values


def diff(old: EmploymentContractVersion, new: EmploymentContractVersion) -> dict[str, list[Any]]:
    """{項目: [変更前, 変更後]}（変わった項目だけ）。随時届出の根拠に使う。"""
    before, after = snapshot(old), snapshot(new)
    return {
        name: [before.get(name), after.get(name)]
        for name in after
        if before.get(name) != after.get(name)
    }


def previous_version(version: EmploymentContractVersion) -> EmploymentContractVersion | None:
    return (
        version.contract.versions.filter(version__lt=version.version).order_by("-version").first()
    )


def monthly_total(version: EmploymentContractVersion) -> int | None:
    """1 か月当たりの支払概算額（別紙1 の 3 = 基本賃金の月額 + 諸手当）。

    月額が分からなければ None。
    """
    base = (
        version.base_wage if version.wage_type == WageType.MONTHLY else version.monthly_equivalent
    )
    if base is None:
        return None
    return base + sum(a.amount for a in version.allowances.all())


def take_home(version: EmploymentContractVersion) -> int | None:
    """手取り支給額（別紙1 の 5 = 支払概算額 − 控除額）。"""
    total = monthly_total(version)
    if total is None:
        return None
    return total - sum(d.amount for d in version.deductions.all())


def equal_pay_evidence_missing(version: EmploymentContractVersion) -> bool:
    """「日本人と同等以上の報酬」の根拠（メモと資料）が無いか（F11 の生成前バリデーション用）。"""
    has_document = version.attachments.filter(kind=AttachmentKind.EQUAL_PAY_EVIDENCE).exists()
    return not version.comparable_japanese_note.strip() or not has_document
