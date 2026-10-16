"""届出・申請タスクの起票と状態の遷移（第5章 F07）。

- 起票は create() に集約し、同じ種別×対象×起算日は 1 件だけにする（何度呼んでもよい）。
- 自動起票: 雇用契約の終了・終了の取り消し・版の追加・新たな契約の締結（services.contracts から
  呼ばれる）と、日付の到来（日次の create_scheduled）。
- 状態の遷移は transition() に集約し、監査ログに「届出タスクの状態変更」として残す。

受入れ困難届出の要否（運用要領 令和8年8月版 第7章第1節第2）:
「特定技能雇用契約を終了する場合、受入れ困難に係る届出書をあらかじめ提出しておかなければ
なりません（自己都合退職を除きます。）」。契約満了（終期到来）も対象外（入管の届出案内）。
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.contracts.models import EmploymentContract, EmploymentContractVersion, TerminationReason
from apps.core.models import Attachment, AuditAction, Organization, Staff, User
from apps.filings.models import Filing, FilingKind, FilingStatus, Trigger
from apps.hosts.models import HostMembership, HostOrganization, SupportContract
from apps.workers.models import (
    ForeignWorker,
    LatestPassportRecord,
    LatestResidenceRecord,
    WorkerStatus,
)
from services import audit, deadlines, filing_rules, hosts
from services.deadlines import Deadline


class Category(enum.StrEnum):
    IMMIGRATION_FILING = "immigration_filing"  # 入管への届出
    IMMIGRATION_APPLICATION = "immigration_application"  # 入管への申請
    INTERNAL = "internal"  # 内部タスク


class Filer(enum.StrEnum):
    HOST = "host"  # 特定技能所属機関（電子届出システムの所属機関用）
    SUPPORT_ORGANIZATION = "support_organization"  # 登録支援機関（自機関）
    INTERNAL = "internal"


CATEGORY_LABELS = {
    Category.IMMIGRATION_FILING: gettext_lazy("immigration notification"),
    Category.IMMIGRATION_APPLICATION: gettext_lazy("immigration application"),
    Category.INTERNAL: gettext_lazy("internal task"),
}
FILER_LABELS = {
    Filer.HOST: gettext_lazy("host organization"),
    Filer.SUPPORT_ORGANIZATION: gettext_lazy("registered support organization"),
    Filer.INTERNAL: gettext_lazy("internal"),
}


@dataclass(frozen=True)
class KindDefinition:
    category: Category
    filer: Filer
    deadline: Callable[[date], Deadline]  # 起算日 → 期限（T10）


KIND_DEFINITIONS: dict[FilingKind, KindDefinition] = {
    FilingKind.RESIDENCE_RENEWAL: KindDefinition(
        Category.IMMIGRATION_APPLICATION, Filer.HOST, deadlines.residence_expiry
    ),
    FilingKind.CONTRACT_CHANGE: KindDefinition(
        Category.IMMIGRATION_FILING, Filer.HOST, deadlines.ad_hoc_filing
    ),
    FilingKind.CONTRACT_TERMINATION: KindDefinition(
        Category.IMMIGRATION_FILING, Filer.HOST, deadlines.ad_hoc_filing
    ),
    FilingKind.CONTRACT_NEW: KindDefinition(
        Category.IMMIGRATION_FILING, Filer.HOST, deadlines.ad_hoc_filing
    ),
    FilingKind.ACCEPTANCE_DIFFICULTY: KindDefinition(
        Category.IMMIGRATION_FILING, Filer.HOST, deadlines.ad_hoc_filing
    ),
    FilingKind.SUPPORT_PLAN_CHANGE: KindDefinition(
        Category.IMMIGRATION_FILING, Filer.HOST, deadlines.ad_hoc_filing
    ),
    FilingKind.MISSING: KindDefinition(
        Category.IMMIGRATION_FILING, Filer.HOST, deadlines.ad_hoc_filing
    ),
    FilingKind.SUPPORT_DIFFICULTY: KindDefinition(
        Category.IMMIGRATION_FILING, Filer.SUPPORT_ORGANIZATION, deadlines.ad_hoc_filing
    ),
    FilingKind.PERIODIC_HOST: KindDefinition(
        Category.IMMIGRATION_FILING, Filer.HOST, lambda basis: deadlines.periodic_filing(basis.year)
    ),
    FilingKind.PERIODIC_SUPPORT_ORGANIZATION: KindDefinition(
        Category.IMMIGRATION_FILING,
        Filer.SUPPORT_ORGANIZATION,
        lambda basis: deadlines.periodic_filing(basis.year),
    ),
    FilingKind.REGISTRATION_RENEWAL: KindDefinition(
        Category.IMMIGRATION_APPLICATION, Filer.SUPPORT_ORGANIZATION, deadlines.registration_renewal
    ),
    FilingKind.MEMBERSHIP_RENEWAL: KindDefinition(
        Category.INTERNAL, Filer.INTERNAL, deadlines.document_expiry
    ),
    FilingKind.PASSPORT_CHECK: KindDefinition(
        Category.INTERNAL, Filer.INTERNAL, deadlines.passport_expiry
    ),
    FilingKind.SUPPORT_CONTRACT_RENEWAL: KindDefinition(
        Category.INTERNAL, Filer.INTERNAL, deadlines.support_contract_end
    ),
    FilingKind.CONTRACT_EXPIRY_CHECK: KindDefinition(
        Category.INTERNAL, Filer.INTERNAL, deadlines.employment_contract_expiry
    ),
}

# 手動で起票できる種別（事由が人の判断で生じるもの）
MANUAL_KINDS: tuple[FilingKind, ...] = (
    FilingKind.ACCEPTANCE_DIFFICULTY,
    FilingKind.SUPPORT_PLAN_CHANGE,
    FilingKind.MISSING,
    FilingKind.SUPPORT_DIFFICULTY,
    FilingKind.CONTRACT_CHANGE,
    FilingKind.CONTRACT_TERMINATION,
    FilingKind.CONTRACT_NEW,
)

# 受入れ困難届出を併せて起票しない終了理由
NO_DIFFICULTY_REASONS = frozenset(
    {TerminationReason.EXPIRY, TerminationReason.VOLUNTARY, TerminationReason.JOB_CHANGE}
)

OPEN_STATUSES = frozenset({FilingStatus.NOT_STARTED, FilingStatus.IN_PROGRESS})

# 状態の遷移（受理済・取消からは変えない）
TRANSITIONS: dict[FilingStatus, frozenset[FilingStatus]] = {
    FilingStatus.NOT_STARTED: frozenset(
        {
            FilingStatus.IN_PROGRESS,
            FilingStatus.SUBMITTED,
            FilingStatus.NOT_REQUIRED,
            FilingStatus.CANCELLED,
        }
    ),
    FilingStatus.IN_PROGRESS: frozenset(
        {
            FilingStatus.NOT_STARTED,
            FilingStatus.SUBMITTED,
            FilingStatus.NOT_REQUIRED,
            FilingStatus.CANCELLED,
        }
    ),
    FilingStatus.SUBMITTED: frozenset({FilingStatus.ACCEPTED, FilingStatus.IN_PROGRESS}),
    FilingStatus.NOT_REQUIRED: frozenset({FilingStatus.NOT_STARTED}),
    FilingStatus.ACCEPTED: frozenset(),
    FilingStatus.CANCELLED: frozenset(),
}


def definition(kind: FilingKind | str) -> KindDefinition:
    return KIND_DEFINITIONS[FilingKind(kind)]


def deadline_of(filing: Filing) -> Deadline:
    """保存した期限を T10 の Deadline に戻す（T14 は assess(deadline_of(f), today) を使う）。"""
    return Deadline(
        kind=deadlines.DeadlineKind(filing.deadline_kind),
        basis_on=filing.basis_on,
        due_on=filing.due_on,
        unadjusted_due_on=filing.unadjusted_due_on,
        opens_on=filing.opens_on,
        final_on=filing.final_on,
    )


# --- 起票 ------------------------------------------------------------------------


def _worker_of(target: models.Model) -> ForeignWorker | None:
    if isinstance(target, ForeignWorker):
        return target
    worker = getattr(target, "worker", None)
    return worker if isinstance(worker, ForeignWorker) else None


def find_open(kind: FilingKind, target: models.Model, basis_on: date) -> Filing | None:
    return (
        Filing.objects.filter(
            kind=kind,
            content_type=ContentType.objects.get_for_model(target),
            object_id=target.pk,
            basis_on=basis_on,
        )
        .exclude(status=FilingStatus.CANCELLED)
        .first()
    )


@transaction.atomic
def create(
    kind: FilingKind,
    *,
    target: models.Model,
    basis_on: date,
    trigger: Trigger,
    by: User | None = None,
    details: dict[str, Any] | None = None,
    needs_review: bool = False,
    assignee: Staff | None = None,
    note: str = "",
) -> tuple[Filing, bool]:
    """起票する。同じ種別×対象×起算日（取消以外）があれば、それを返して作らない。"""
    existing = find_open(kind, target, basis_on)
    if existing is not None:
        return existing, False
    deadline = definition(kind).deadline(basis_on)
    worker = _worker_of(target)
    filing = Filing(
        kind=kind,
        content_type=ContentType.objects.get_for_model(target),
        object_id=target.pk,
        target_repr=str(target)[:255],
        worker=worker,
        trigger=trigger,
        trigger_details=details or {},
        needs_review=needs_review,
        deadline_kind=deadline.kind,
        basis_on=basis_on,
        opens_on=deadline.opens_on,
        due_on=deadline.due_on,
        unadjusted_due_on=deadline.unadjusted_due_on,
        final_on=deadline.final_on,
        assignee=assignee or (worker.assigned_staff if worker else None),
        note=note,
        created_by=by,
        updated_by=by,
    )
    filing.save()
    return filing, True


def create_if_active(
    kind: FilingKind, *, target: models.Model, **kwargs: Any
) -> tuple[Filing | None, bool]:
    """自動の起票の入口。無効化した外国人の届出は作らない（T13e）。手での起票は create()。"""
    worker = _worker_of(target)
    if worker is not None and worker.deactivated_at is not None:
        return None, False
    return create(kind, target=target, **kwargs)


def on_contract_terminated(contract: EmploymentContract, *, by: User | None) -> list[Filing]:
    """契約の終了 → 終了届出。

    会社都合・その他なら受入れ困難届出も起票する（自己都合退職・契約満了・転職は除く）。
    """
    assert contract.terminated_on is not None
    details = {
        "termination_reason": contract.termination_reason,
        "termination_note": contract.termination_note,
    }
    candidates = [
        create_if_active(
            FilingKind.CONTRACT_TERMINATION,
            target=contract,
            basis_on=contract.terminated_on,
            trigger=Trigger.CONTRACT_TERMINATED,
            by=by,
            details=details,
        )[0]
    ]
    if contract.termination_reason not in NO_DIFFICULTY_REASONS:
        candidates.append(
            create_if_active(
                FilingKind.ACCEPTANCE_DIFFICULTY,
                target=contract,
                basis_on=contract.terminated_on,
                trigger=Trigger.CONTRACT_TERMINATED,
                by=by,
                details=details,
                note=_(
                    "The basis date is the termination date. If the event (e.g. notice of "
                    "dismissal) occurred earlier, correct the basis date."
                ),
            )[0]
        )
    created = [filing for filing in candidates if filing is not None]
    return created


def on_termination_cancelled(
    contract: EmploymentContract, *, terminated_on: date, by: User | None
) -> None:
    """終了の取り消し → 起票済みの終了届出・受入れ困難届出を取消にする。

    提出済み以降のものは取消にせず、メモに残す。
    """
    reason = _("The termination of the employment contract was cancelled.")
    for kind in (FilingKind.CONTRACT_TERMINATION, FilingKind.ACCEPTANCE_DIFFICULTY):
        filing = find_open(kind, contract, terminated_on)
        if filing is None:
            continue
        if FilingStatus.CANCELLED in TRANSITIONS[FilingStatus(filing.status)]:
            transition(filing, FilingStatus.CANCELLED, by=by, reason=reason)
        else:
            with audit.event(AuditAction.FILING_TRANSITION, target=filing, actor=by):
                filing.note = f"{filing.note}\n{reason}".strip()
                filing.updated_by = by
                filing.save(update_fields=["note", "updated_by", "updated_at"])


def on_version_added(version: EmploymentContractVersion, *, by: User | None) -> Filing | None:
    """版の追加 → 届出が必要な変更（または要否の確認が必要な変更）があれば変更届出。

    1 版目なら、同じ受入れ機関との終了済みの契約がある場合に新たな契約締結の届出。
    明細（手当・控除など）と業務区分を保存した後に呼ぶ。
    """
    from services import contracts  # services.contracts が本モジュールを呼ぶため循環を避ける

    contract = version.contract
    previous = contracts.previous_version(version)
    if previous is None:
        return _on_contract_concluded(contract, version, by=by)
    findings = filing_rules.contract_change_findings(
        contracts.snapshot(previous), contracts.snapshot(version)
    )
    if not findings.needs_filing:
        return None
    details = findings.as_details() | {
        "version": version.version,
        "previous_version": previous.version,
    }
    filing, _created = create_if_active(
        FilingKind.CONTRACT_CHANGE,
        target=contract,
        basis_on=version.changed_on,
        trigger=Trigger.CONTRACT_CHANGED,
        by=by,
        details=details,
        needs_review=not findings.required,
    )
    return filing


def _on_contract_concluded(
    contract: EmploymentContract, version: EmploymentContractVersion, *, by: User | None
) -> Filing | None:
    """同じ外国人と同じ受入れ機関が、終了した契約の後に再び契約した場合だけ届出が必要。

    別の受入れ機関との契約は在留資格変更許可申請になるため、ここでは起票しない。
    """
    rehired = (
        EmploymentContract.objects.filter(
            worker=contract.worker,
            host_organization=contract.host_organization,
            terminated_on__isnull=False,
        )
        .exclude(pk=contract.pk)
        .exists()
    )
    if not rehired:
        return None
    filing, _created = create_if_active(
        FilingKind.CONTRACT_NEW,
        target=contract,
        basis_on=version.signed_on or version.starts_on,
        trigger=Trigger.CONTRACT_CONCLUDED,
        by=by,
    )
    return filing


# --- 日次の起票 ----------------------------------------------------------------------


@dataclass
class ScheduledResult:
    created: list[Filing]
    existing: int


def create_scheduled(today: date, *, by: User | None = None) -> ScheduledResult:
    """日付の到来で起票するもの。受付・着手の日を過ぎていて、まだ無いものだけ作る（冪等）。"""
    result = ScheduledResult(created=[], existing=0)

    def add(kind: FilingKind, target: models.Model, basis_on: date, trigger: Trigger) -> None:
        deadline = definition(kind).deadline(basis_on)
        if deadline.opens_on is not None and today < deadline.opens_on:
            return
        filing, created = create_if_active(
            kind, target=target, basis_on=basis_on, trigger=trigger, by=by
        )
        if filing is None:
            return
        if created:
            result.created.append(filing)
        else:
            result.existing += 1

    # 無効化した外国人は対象にしない（念のため create_if_active でも外す）
    for latest in LatestResidenceRecord.objects.select_related("record", "worker").filter(
        worker__status=WorkerStatus.RESIDING, worker__deactivated_at__isnull=True
    ):
        add(
            FilingKind.RESIDENCE_RENEWAL,
            latest.record,
            latest.record.expires_on,
            Trigger.RESIDENCE_EXPIRY,
        )

    for latest_passport in LatestPassportRecord.objects.select_related("record").filter(
        worker__status__in=[WorkerStatus.PRE_ENTRY, WorkerStatus.RESIDING],
        worker__deactivated_at__isnull=True,
    ):
        add(
            FilingKind.PASSPORT_CHECK,
            latest_passport.record,
            latest_passport.record.expires_on,
            Trigger.PASSPORT_EXPIRY,
        )

    organization = Organization.objects.first()
    if organization is not None:
        add(
            FilingKind.REGISTRATION_RENEWAL,
            organization,
            organization.registration_expires_on,
            Trigger.REGISTRATION_EXPIRY,
        )

    for membership in HostMembership.objects.filter(valid_until__isnull=False):
        assert membership.valid_until is not None
        add(
            FilingKind.MEMBERSHIP_RENEWAL,
            membership,
            membership.valid_until,
            Trigger.DOCUMENT_EXPIRY,
        )
    for attachment in _latest_attachments_with_expiry():
        assert attachment.valid_until is not None
        add(
            FilingKind.MEMBERSHIP_RENEWAL,
            attachment,
            attachment.valid_until,
            Trigger.DOCUMENT_EXPIRY,
        )

    for support_contract in SupportContract.objects.filter(ends_on__isnull=False):
        assert support_contract.ends_on is not None
        renewed = SupportContract.objects.filter(
            host_organization_id=support_contract.host_organization_id,
            starts_on__gt=support_contract.ends_on,
        ).exists()
        if not renewed:
            add(
                FilingKind.SUPPORT_CONTRACT_RENEWAL,
                support_contract,
                support_contract.ends_on,
                Trigger.SUPPORT_CONTRACT_END,
            )

    for contract in EmploymentContract.objects.filter(
        terminated_on__isnull=True,
        current_ends_on__isnull=False,
        worker__deactivated_at__isnull=True,
    ):
        assert contract.current_ends_on is not None
        add(
            FilingKind.CONTRACT_EXPIRY_CHECK,
            contract,
            contract.current_ends_on,
            Trigger.CONTRACT_EXPIRY,
        )

    # 定期届出は届出期間（4/1〜5/31）の間だけ起票する（運用開始前の年の分を後から作らない）
    season = date(today.year, 4, 1)
    if season <= today <= date(today.year, 5, 31):
        for host in _hosts_with_active_support_contract(season):
            add(FilingKind.PERIODIC_HOST, host, season, Trigger.PERIODIC_SEASON)
        if organization is not None:
            add(
                FilingKind.PERIODIC_SUPPORT_ORGANIZATION,
                organization,
                season,
                Trigger.PERIODIC_SEASON,
            )
    return result


def _latest_attachments_with_expiry() -> list[Attachment]:
    """同じ対象・同じ種別の添付のうち最新のもの（差し替え前の古い証明書は見ない）。

    加入状況（HostMembership）の証明書は、加入状況の有効期限で起票するので対象外にする
    （同じ証明書のタスクを 2 件作らないため）。
    """
    membership_type = ContentType.objects.get_for_model(HostMembership)
    latest: dict[tuple[int, int, str], Attachment] = {}
    for attachment in Attachment.objects.exclude(content_type=membership_type).order_by(
        "created_at", "pk"
    ):
        latest[(attachment.content_type_id, attachment.object_id, attachment.kind)] = attachment
    return [a for a in latest.values() if a.valid_until is not None]


def _hosts_with_active_support_contract(on: date) -> Iterable[HostOrganization]:
    return [
        host
        for host in HostOrganization.objects.filter(support_contracts__isnull=False).distinct()
        if hosts.active_contract(host, on) is not None
    ]


# --- 状態の遷移・起算日の訂正 --------------------------------------------------------------


def transition(
    filing: Filing,
    to: FilingStatus,
    *,
    by: User | None,
    submitted_on: date | None = None,
    receipt_number: str = "",
    reason: str = "",
) -> None:
    """状態を変える。許されない遷移・必須の項目の不足は ValidationError。監査ログに 1 件残す。"""
    if to == FilingStatus(filing.status):
        return
    validate_transition(
        filing, to, submitted_on=submitted_on, receipt_number=receipt_number, reason=reason
    )
    with audit.event(AuditAction.FILING_TRANSITION, target=filing, actor=by):
        filing.status = to
        if submitted_on is not None:
            filing.submitted_on = submitted_on
        if receipt_number:
            filing.receipt_number = receipt_number
        if reason:
            filing.status_reason = reason
        filing.updated_by = by
        filing.save()


def validate_transition(
    filing: Filing,
    to: FilingStatus,
    *,
    submitted_on: date | None = None,
    receipt_number: str = "",
    reason: str = "",
) -> None:
    current = FilingStatus(filing.status)
    if to not in TRANSITIONS[current]:
        raise ValidationError(
            _("The status cannot be changed from %(from)s to %(to)s.")
            % {"from": current.label, "to": to.label},
            code="transition",
        )
    if to is FilingStatus.SUBMITTED and (submitted_on or filing.submitted_on) is None:
        raise ValidationError(_("Enter the submission date."), code="submitted_on")
    if to is FilingStatus.ACCEPTED and not (receipt_number or filing.receipt_number).strip():
        raise ValidationError(_("Enter the receipt number."), code="receipt_number")
    if to in (FilingStatus.NOT_REQUIRED, FilingStatus.CANCELLED) and not reason.strip():
        raise ValidationError(_("Enter the reason."), code="reason")


def rebase(filing: Filing, basis_on: date, *, by: User | None, reason: str) -> None:
    """起算日（事由の日）を直して期限を計算し直す（例: 受入れ困難の事由が終了日より前）。"""
    if not reason.strip():
        raise ValidationError(_("Enter the reason."), code="reason")
    if FilingStatus(filing.status) not in OPEN_STATUSES:
        raise ValidationError(_("Only open tasks can be changed."), code="closed")
    deadline = definition(filing.kind).deadline(basis_on)
    with audit.event(
        AuditAction.FILING_TRANSITION, target=filing, actor=by, context={"reason": reason}
    ):
        filing.basis_on = basis_on
        filing.opens_on = deadline.opens_on
        filing.due_on = deadline.due_on
        filing.unadjusted_due_on = deadline.unadjusted_due_on
        filing.final_on = deadline.final_on
        filing.updated_by = by
        filing.save()


# --- 業務画面と admin の共通（T14）------------------------------------------------------


def update_details(
    filing: Filing,
    *,
    by: User | None,
    assignee: Staff | None,
    note: str,
    submitted_on: date | None = None,
    receipt_number: str | None = None,
) -> bool:
    """担当者・メモと、提出日・受理番号の書き直し（状態は変えない）。変わったときだけ保存して True。

    提出日・受理番号は None なら変えない。監査ログには届出タスクの更新として変更前後が残る。
    """
    values: dict[str, Any] = {"assignee_id": assignee.pk if assignee else None, "note": note}
    if submitted_on is not None:
        values["submitted_on"] = submitted_on
    if receipt_number is not None:
        values["receipt_number"] = receipt_number.strip()
    changed = [name for name, value in values.items() if getattr(filing, name) != value]
    if not changed:
        return False
    for name in changed:
        setattr(filing, name, values[name])
    filing.updated_by = by
    filing.save(
        update_fields=[name.removesuffix("_id") for name in changed] + ["updated_by", "updated_at"]
    )
    return True


def manual_target(
    *,
    worker: ForeignWorker | None,
    contract: EmploymentContract | None,
    host: HostOrganization | None,
) -> models.Model:
    """手動の起票の対象（外国人・雇用契約・受入れ機関のちょうど 1 つ）。無効化済みの人は不可。"""
    targets = [target for target in (contract, worker, host) if target is not None]
    if len(targets) != 1:
        raise ValidationError(_("Select exactly one target."), code="target")
    target = targets[0]
    owner = _worker_of(target)
    if owner is not None and owner.deactivated_at is not None:
        raise ValidationError(
            _("Filing tasks cannot be created for a deactivated foreign worker."),
            code="deactivated",
        )
    return target
