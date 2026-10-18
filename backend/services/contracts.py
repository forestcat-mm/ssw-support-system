"""雇用契約の業務ロジック（第5章 F03・F07、第7章 employment_contracts）。

- 版の追加: 新しい版を作り、見出しの現在の期間を同じトランザクションで更新する。
- 状態（予定／有効／終了）は列で持たず、日付と終了の登録から決める。
- 期間の重なり・派遣の不可（事業所は契約相手の受入れ機関のもの）・業務区分は保存前に検査する
  （DB の排他制約もあるが、admin のインラインでは制約の検査が飛ばされるため事前に見る）。
- 版どうしの差分（diff）は T11 の随時届出の根拠になる。
- 業務画面（T28b）と admin の版の検査は validate_version に集める。業務画面の版の追加は
  create_version（明細まで 1 つのトランザクションで保存し、前の版と差分が無ければ保存しない）。
- 版の項目は参考様式第1-6号（001472154.docx、SHA-256 0706326a…）を基準にする（ADR-0016）。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Max, Q
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.contracts.models import (
    EmploymentContract,
    EmploymentContractAllowance,
    EmploymentContractDeduction,
    EmploymentContractShift,
    EmploymentContractVersion,
    Renewal,
    RenewalCriterion,
    TerminationReason,
    WageType,
)
from apps.core.models import AttachmentKind, AuditAction, User
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


def _site_or_none(version: EmploymentContractVersion) -> HostSite | None:
    return version.site if version.site_id else None


def validate_change(contract: EmploymentContract | None, change_reason: str) -> None:
    """2 版目以降は変更理由が必須。"""
    if contract is not None and contract.versions.exists() and not change_reason.strip():
        raise ValidationError(_("Enter the reason for the change."), code="change_reason")


def validate_same_employer(contract: EmploymentContract | None, site: HostSite | None) -> None:
    """新しい版の事業所は、契約の受入れ機関のもの（雇用主が変わるなら新しい契約。T28b の D6）。"""
    if contract is not None and site is not None:
        if site.host_organization_id != contract.host_organization_id:
            raise ValidationError(
                _(
                    "Select a site of the host organization of this contract. If the employer "
                    "changes, register a new contract."
                ),
                code="other_employer",
            )


def conditional_errors(version: EmploymentContractVersion) -> dict[str, list[str]]:
    """条件つきの必須と組み合わせの検査（雇用条件書 Ⅰ・Ⅵ・Ⅶ の「有」のときの中身など）。"""
    errors: dict[str, list[str]] = {}

    def add(field: str, message: str) -> None:
        errors.setdefault(field, []).append(message)

    criteria = list(version.renewal_criteria or [])
    if version.renewal == Renewal.POSSIBLE and not criteria:
        add("renewal_criteria", _("Select the criteria for renewal."))
    if version.renewal != Renewal.POSSIBLE and criteria:
        add("renewal_criteria", _("Criteria are only for “may be renewed”."))
    if RenewalCriterion.OTHER in criteria and not version.renewal_criteria_other.strip():
        add("renewal_criteria_other", _("Enter the other criteria."))
    if version.has_renewal_limit and not (
        version.renewal_limit_count or version.renewal_limit_years
    ):
        add("renewal_limit_count", _("Enter the number of renewals or the total years."))
    if version.has_early_paid_leave and not (
        version.early_paid_leave_months and version.early_paid_leave_days
    ):
        add("early_paid_leave_days", _("Enter after how many months and how many days."))
    for flag, note in (
        ("has_raise", "raise_note"),
        ("has_bonus", "bonus_note"),
        ("has_retirement_allowance", "retirement_allowance_note"),
    ):
        if getattr(version, flag) and not getattr(version, note).strip():
            add(note, _("Enter the timing and amount."))
    # 無期転換（期間の定めの無い契約には欄が無い。転換の日は終了日の翌日）
    if version.indefinite_conversion:
        if version.ends_on is None:
            add(
                "indefinite_conversion",
                _("A contract without an end date cannot be converted to an indefinite one."),
            )
        if not version.conversion_conditions_change:
            add(
                "conversion_conditions_change",
                _("Select whether the working conditions change after conversion."),
            )
    elif version.conversion_conditions_change:
        add(
            "conversion_conditions_change",
            _("Only when the worker can apply for an indefinite contract."),
        )
    return errors


def validate_version(
    version: EmploymentContractVersion,
    *,
    contract: EmploymentContract | None,
    worker: ForeignWorker | None,
    host_organization: HostOrganization | None,
    categories: Iterable[OperationCategory],
) -> None:
    """版を保存する前の検査（業務画面と admin で共通）。エラーは欄ごとの ValidationError。"""
    host = contract.host_organization if contract is not None else host_organization
    worker = contract.worker if contract is not None else worker
    site = _site_or_none(version)
    errors: dict[str, list[Any]] = {}
    checks: list[tuple[str, Any]] = [
        ("site", lambda: validate_site_belongs_to_host(host, site)),
        ("site", lambda: validate_same_employer(contract, site)),
        ("categories", lambda: validate_categories_of_site(site, categories)),
        ("change_reason", lambda: validate_change(contract, version.change_reason or "")),
        (
            "starts_on",
            lambda: validate_no_overlap(
                getattr(worker, "pk", None),
                version.starts_on,
                version.ends_on,
                contract.terminated_on if contract else None,
                exclude_contract_id=contract.pk if contract else None,
            ),
        ),
    ]
    for field, check in checks:
        try:
            check()
        except ValidationError as error:
            errors.setdefault(field, []).extend(error.messages)
    for field, messages in conditional_errors(version).items():
        errors.setdefault(field, []).extend(messages)
    if errors:
        raise ValidationError(errors)


def saved_version_errors(version: EmploymentContractVersion) -> dict[str, list[str]]:
    """保存済みの版が、今の検査（validate_version と同じ規則）に合っているか。

    期間の重なりは DB の排他制約で保証されるので見ない。第1版は変更理由を求めない。
    ダミーデータ・移行の確かめに使う。
    """
    contract = version.contract
    site = _site_or_none(version)
    errors: dict[str, list[str]] = {}
    checks: list[tuple[str, Any]] = [
        ("site", lambda: validate_site_belongs_to_host(contract.host_organization, site)),
        ("categories", lambda: validate_categories_of_site(site, version.categories.all())),
    ]
    if version.version > 1:
        checks.append(
            ("change_reason", lambda: validate_change(contract, version.change_reason or ""))
        )
    for field, check in checks:
        try:
            check()
        except ValidationError as error:
            errors.setdefault(field, []).extend(error.messages)
    for field, messages in conditional_errors(version).items():
        errors.setdefault(field, []).extend(messages)
    return errors


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


@dataclass(frozen=True)
class Details:
    """版の明細行（保存前。version は create_version が入れる）。"""

    shifts: Sequence[EmploymentContractShift] = ()
    allowances: Sequence[EmploymentContractAllowance] = ()
    deductions: Sequence[EmploymentContractDeduction] = ()


@dataclass(frozen=True)
class VersionSaved:
    version: EmploymentContractVersion
    filing: Any  # 起票した届出タスク（Filing。無ければ None）
    conversion_warning: str  # 無期転換の通算期間の警告（無ければ空）


@transaction.atomic
def create_version(
    version: EmploymentContractVersion,
    *,
    contract: EmploymentContract | None,
    worker: ForeignWorker | None = None,
    host_organization: HostOrganization | None = None,
    categories: Iterable[OperationCategory],
    details: Details,
    by: User | None,
) -> VersionSaved:
    """版（と明細・業務区分）を 1 つのトランザクションで保存し、届出を起票する（業務画面）。

    2 版目以降で前の版と差分が無ければ保存しない（ValidationError。すべて巻き戻す）。
    """
    categories = list(categories)
    validate_version(
        version,
        contract=contract,
        worker=worker,
        host_organization=host_organization,
        categories=categories,
    )
    add_version(
        version,
        contract=contract,
        worker=worker,
        host_organization=host_organization,
        by=by,
    )
    version.categories.set(categories)
    for rows in (details.shifts, details.allowances, details.deductions):
        for row in rows:
            row.version = version
            row.created_by = by
            row.updated_by = by
            row.save()
    previous = previous_version(version)
    if previous is not None and not diff(previous, version):
        raise ValidationError(
            _("There is no change from the previous version. Change at least one item."),
            code="no_change",
        )
    filing = complete_version(version, by=by)
    return VersionSaved(
        version=version, filing=filing, conversion_warning=conversion_warning(version)
    )


VERSION_ATTACHMENT_KINDS = (
    AttachmentKind.EQUAL_PAY_EVIDENCE,
    AttachmentKind.VARIABLE_HOURS_DOCUMENTS,
    AttachmentKind.INDEFINITE_CONVERSION_TERMS,
)


@transaction.atomic
def add_version_attachments(
    version: EmploymentContractVersion, kind: str, files: Iterable[Any], *, by: User | None
) -> list[Any]:
    """版の根拠資料（同等報酬・変形労働時間制・別紙2）を足す。版の内容は変えない。"""
    from services import attachments

    if kind not in VERSION_ATTACHMENT_KINDS:
        raise ValidationError({"kind": _("Select a valid choice.")})
    files = list(files)
    if not files:
        raise ValidationError({"uploads": _("Select a file.")})
    messages: list[str] = []
    for file in files:
        try:
            attachments.validate_upload(kind, file)
        except ValidationError as error:
            messages.extend(error.messages)
    if messages:
        raise ValidationError({"uploads": messages})
    return [
        attachments.create_attachment(
            target=version, kind=AttachmentKind(kind), file=file, created_by=by
        )
        for file in files
    ]


def conversion_terms_missing(version: EmploymentContractVersion) -> bool:
    """無期転換後の労働条件が「変更あり」なのに、別紙2 の添付が無いか（画面の注意）。"""
    from apps.contracts.models import ConversionChange as Change

    if version.conversion_conditions_change != Change.CHANGED:
        return False
    return not version.attachments.filter(
        kind=AttachmentKind.INDEFINITE_CONVERSION_TERMS, voided_at__isnull=True
    ).exists()


def initial_from(previous: EmploymentContractVersion) -> dict[str, Any]:
    """新しい版の入力の初期値（前の版を写す。T28b の D4）。

    写さない項目: 変更日・変更理由・締結日（変更の合意の日）。根拠資料の添付も写さない
    （新しい版で付け直す。前の版の添付は画面で参照として出す）。
    """
    skip = {"id", "contract", "version", "change_reason", "changed_on", "signed_on"}
    skip |= {"created_at", "updated_at", "created_by", "updated_by"}
    values: dict[str, Any] = {}
    for field in previous._meta.concrete_fields:
        if field.name in skip:
            continue
        values[field.name] = field.value_from_object(previous)
    values["site"] = previous.site_id
    values["categories"] = list(previous.categories.all())
    return values


def detail_initial(previous: EmploymentContractVersion) -> dict[str, list[dict[str, Any]]]:
    """明細の行の初期値（前の版を写す）。"""
    return {
        "shifts": [
            {
                "start_time": s.start_time,
                "end_time": s.end_time,
                "applies_to": s.applies_to,
                "daily_working_minutes": s.daily_working_minutes,
            }
            for s in previous.shifts.order_by("start_time", "pk")
        ],
        "allowances": [
            {
                "name": a.name,
                "amount": a.amount,
                "calculation_method": a.calculation_method,
                "fixed_overtime_hours": a.fixed_overtime_hours,
            }
            for a in previous.allowances.order_by("pk")
        ],
        "deductions": [
            {"kind": d.kind, "name": d.name, "amount": d.amount}
            for d in previous.deductions.order_by("pk")
        ],
    }


# --- 無期転換の通算期間（目安。T28b の D1、ADR-0016）-----------------------------------

CONVERSION_YEARS = 5


def conversion_warning(version: EmploymentContractVersion) -> str:
    """同じ受入れ機関との契約の通算の期間が 5 年を超えるのに、無期転換の印が無ければ警告の文言。

    数えるのはシステムに登録した同じ受入れ機関の契約だけ（技能実習など登録の外の期間は入らない）。
    空白期間（労働契約法第18条第2項のクーリング）は判定しない。期間の定めの無い版は対象外。
    """
    if version.indefinite_conversion or version.ends_on is None:
        return ""
    contract = version.contract
    total = timedelta(0)
    for other in EmploymentContract.objects.filter(
        worker_id=contract.worker_id, host_organization_id=contract.host_organization_id
    ):
        first = other.versions.order_by("version").values_list("starts_on", flat=True).first()
        start = first or other.current_starts_on
        end = (
            version.ends_on
            if other.pk == contract.pk
            else _effective_end(other.current_ends_on, other.terminated_on)
        )
        if end is None:
            return ""
        total += end - start + timedelta(days=1)
    # 5 年（閏日を 1 日含めた日数）を超えるか。登録の外の期間を数えない目安なので日数で見る
    if total <= timedelta(days=365 * CONVERSION_YEARS + 1):
        return ""
    return _(
        "The total period of the contracts with this host organization registered in the "
        "system exceeds 5 years. Check whether the worker can apply for an indefinite contract "
        "(Labor Contract Act, Article 18). Periods outside the system (such as technical "
        "intern training) are not counted, and gaps between contracts (cooling-off periods) "
        "are not judged."
    )


@transaction.atomic
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
    from services import filings  # services.filings が本モジュールを呼ぶため循環を避ける

    filings.on_contract_terminated(contract, by=by)


@transaction.atomic
def cancel_termination(contract: EmploymentContract, *, by: User | None, reason: str) -> list[Any]:
    """終了の登録を取り消す（入力ミスの訂正。管理者のみ。理由は必須。期間の重なりを確かめる）。

    理由は監査ログ（見出しの更新の context）と、戻した終了届出のタスクに残す。起票済みのタスクは
    共通の規則（services.filings.revert_raised_task）で戻し、対応中として残したタスクを返す。
    """
    if not reason.strip():
        raise ValidationError({"reason": _("Enter the reason.")}, code="reason")
    validate_no_overlap(
        contract.worker_id,
        contract.current_starts_on,
        contract.current_ends_on,
        None,
        exclude_contract_id=contract.pk,
    )
    terminated_on = contract.terminated_on
    from services import audit, filings

    with audit.event(
        AuditAction.UPDATE, target=contract, actor=by, context={"cancel_termination": reason}
    ):
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
    if terminated_on is None:
        return []
    return filings.on_termination_cancelled(
        contract, terminated_on=terminated_on, by=by, reason=reason
    )


def complete_version(version: EmploymentContractVersion, *, by: User | None) -> Any:
    """版の明細・業務区分を保存し終えた後に呼ぶ（変更届出・新たな契約締結の届出を起票する）。

    起票した届出タスク（無ければ None）を返す。
    """
    from services import filings

    return filings.on_version_added(version, by=by)


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
    has_document = version.attachments.filter(
        kind=AttachmentKind.EQUAL_PAY_EVIDENCE, voided_at__isnull=True
    ).exists()
    return not version.comparable_japanese_note.strip() or not has_document
