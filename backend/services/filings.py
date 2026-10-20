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
from collections.abc import Callable, Iterable, Iterator
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
from apps.hosts.models import (
    HostCooperationConfirmation,
    HostMembership,
    HostOrganization,
    SupportContract,
    SupportTerminationCategory,
    SupportTerminationReason,
)
from apps.support.models import PeriodicInterview
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


def _signed_copy_deadline(basis_on: date) -> Deadline:
    """署名済み書類の回収（生成日から自機関の設定の日数。T33、ADR-0022）。"""
    organization = Organization.objects.first()
    days = organization.signed_copy_due_days if organization is not None else 14
    return deadlines.signed_copy(basis_on, days)


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
    # 基準不適合（第3-5号）・支援計画の実施困難（第3-7号）。手で起票する（T50-D7）
    FilingKind.STANDARD_NONCONFORMITY: KindDefinition(
        Category.IMMIGRATION_FILING, Filer.HOST, deadlines.ad_hoc_filing
    ),
    FilingKind.SUPPORT_PLAN_DIFFICULTY: KindDefinition(
        Category.IMMIGRATION_FILING, Filer.HOST, deadlines.ad_hoc_filing
    ),
    FilingKind.SUPPORT_DIFFICULTY: KindDefinition(
        Category.IMMIGRATION_FILING, Filer.SUPPORT_ORGANIZATION, deadlines.ad_hoc_filing
    ),
    # 支援委託契約の変更（第3-3-1号）・終了又は締結（第3-3-2号）。所属機関が外国人ごと（T28c）
    FilingKind.SUPPORT_CONTRACT_CHANGE: KindDefinition(
        Category.IMMIGRATION_FILING, Filer.HOST, deadlines.ad_hoc_filing
    ),
    FilingKind.SUPPORT_CONTRACT_END_OR_CONCLUSION: KindDefinition(
        Category.IMMIGRATION_FILING, Filer.HOST, deadlines.ad_hoc_filing
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
    # 起算日は前回の面談日（無ければ今の雇用契約の開始日）。次回の予定日は trigger_details に持つ
    FilingKind.PERIODIC_INTERVIEW: KindDefinition(
        Category.INTERNAL, Filer.INTERNAL, deadlines.periodic_interview
    ),
    # 起算日は生成日。署名済みの添付で自動で完了し、取消はさせない（「不要」で閉じる。T33）
    FilingKind.SIGNED_COPY_COLLECTION: KindDefinition(
        Category.INTERNAL, Filer.INTERNAL, _signed_copy_deadline
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
    FilingKind.SUPPORT_CONTRACT_CHANGE,
    FilingKind.SUPPORT_CONTRACT_END_OR_CONCLUSION,
    FilingKind.STANDARD_NONCONFORMITY,
    FilingKind.SUPPORT_PLAN_DIFFICULTY,
)

# 受入れ困難届出を併せて起票しない終了理由
NO_DIFFICULTY_REASONS = frozenset(
    {TerminationReason.EXPIRY, TerminationReason.VOLUNTARY, TerminationReason.JOB_CHANGE}
)

OPEN_STATUSES = frozenset({FilingStatus.NOT_STARTED, FilingStatus.IN_PROGRESS})

# 状態の遷移（受理済・取消からは変えない）。完了は内部のタスクだけ（allowed_transitions で絞る）
TRANSITIONS: dict[FilingStatus, frozenset[FilingStatus]] = {
    FilingStatus.NOT_STARTED: frozenset(
        {
            FilingStatus.IN_PROGRESS,
            FilingStatus.SUBMITTED,
            FilingStatus.NOT_REQUIRED,
            FilingStatus.CANCELLED,
            FilingStatus.COMPLETED,
        }
    ),
    FilingStatus.IN_PROGRESS: frozenset(
        {
            FilingStatus.NOT_STARTED,
            FilingStatus.SUBMITTED,
            FilingStatus.NOT_REQUIRED,
            FilingStatus.CANCELLED,
            FilingStatus.COMPLETED,
        }
    ),
    FilingStatus.SUBMITTED: frozenset({FilingStatus.ACCEPTED, FilingStatus.IN_PROGRESS}),
    FilingStatus.NOT_REQUIRED: frozenset({FilingStatus.NOT_STARTED}),
    FilingStatus.ACCEPTED: frozenset(),
    FilingStatus.CANCELLED: frozenset(),
    # 完了の取り消し（やり直し）。理由は必須
    FilingStatus.COMPLETED: frozenset({FilingStatus.IN_PROGRESS, FilingStatus.NOT_STARTED}),
}

# 記録を通してだけ完了・完了の取り消しをする種別（手では変えない）
COMPLETED_BY_RECORD = frozenset({FilingKind.PERIODIC_INTERVIEW, FilingKind.SIGNED_COPY_COLLECTION})
# 手では取り消さない種別（「不要」で閉じる。元の記録の取消のときだけ記録を通して取り消す）
CANCELLED_BY_RECORD_ONLY = frozenset({FilingKind.SIGNED_COPY_COLLECTION})


def definition(kind: FilingKind | str) -> KindDefinition:
    return KIND_DEFINITIONS[FilingKind(kind)]


def allowed_transitions(filing: Filing, *, by_record: bool = False) -> frozenset[FilingStatus]:
    """今の状態から行ける状態。完了は内部のタスクだけ。定期面談の完了と取り消しは記録を通してだけ。"""
    current = FilingStatus(filing.status)
    allowed = set(TRANSITIONS[current])
    if definition(filing.kind).category is not Category.INTERNAL:
        allowed.discard(FilingStatus.COMPLETED)
    if FilingKind(filing.kind) in COMPLETED_BY_RECORD and not by_record:
        allowed.discard(FilingStatus.COMPLETED)
        if current is FilingStatus.COMPLETED:
            allowed.clear()
    if FilingKind(filing.kind) in CANCELLED_BY_RECORD_ONLY and not by_record:
        allowed.discard(FilingStatus.CANCELLED)
    return frozenset(allowed)


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
    """同じ種別・対象・起算日の取消以外のタスク（閉じたタスクも含む）。"""
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
def find_unfinished(kind: FilingKind, target: models.Model, basis_on: date) -> Filing | None:
    """同じ種別・対象・起算日の未完了（未着手・対応中）のタスク。"""
    return (
        Filing.objects.filter(
            kind=kind,
            content_type=ContentType.objects.get_for_model(target),
            object_id=target.pk,
            basis_on=basis_on,
            status__in=list(OPEN_STATUSES),
        )
        .order_by("pk")
        .first()
    )


def _other_unfinished_exists(filing: Filing, basis_on: date) -> bool:
    return (
        Filing.objects.filter(
            kind=filing.kind,
            content_type_id=filing.content_type_id,
            object_id=filing.object_id,
            basis_on=basis_on,
            status__in=list(OPEN_STATUSES),
        )
        .exclude(pk=filing.pk)
        .exists()
    )


def create(
    kind: FilingKind,
    *,
    target: models.Model,
    basis_on: date,
    trigger: Trigger,
    by: User | None = None,
    reuse_closed: bool = True,
    details: dict[str, Any] | None = None,
    needs_review: bool = False,
    assignee: Staff | None = None,
    note: str = "",
    deadline: Deadline | None = None,
) -> tuple[Filing, bool]:
    """起票する。同じ種別×対象×起算日（取消以外）があれば、それを返して作らない。

    reuse_closed=False は未完了のタスクだけを再利用し、閉じたタスクしか無ければ新しく起票する
    （第4-3号の自動の起票。ADR-0012）。
    deadline は種別の既定の計算と違う期限（定期面談の次回の予定日など）を使うときだけ渡す。
    """
    existing = (
        find_open(kind, target, basis_on)
        if reuse_closed
        else find_unfinished(kind, target, basis_on)
    )
    if existing is not None:
        return existing, False
    deadline = deadline or definition(kind).deadline(basis_on)
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
    details: dict[str, Any] = {
        "termination_reason": contract.termination_reason,
        "termination_note": contract.termination_note,
    }
    # 外国人の退職で支援委託契約も終わる場合は、第3-1-2号の c 欄に記入して第3-3-2号に代える
    # （運用要領 本体 印刷 115頁。ADR-0017）
    support = hosts.active_contract(contract.host_organization, contract.terminated_on)
    if support is not None:
        details["support_contract"] = support.pk
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


# --- 元の事由の取消（共通の規則。ADR-0017 の決定 6）-------------------------------------

# 元の事由の種類 → タスクに出す文言（task_notes）
SOURCE_CANCELLED_LABELS = {
    "contract_termination": gettext_lazy("the termination of the employment contract"),
    "support_contract_termination": gettext_lazy("the termination of the support contract"),
    "briefing": gettext_lazy("the record of the guidance or orientation"),
}


class Reverted(enum.StrEnum):
    CANCELLED = "cancelled"  # 未着手だったので取消にした
    LEFT_OPEN = "left_open"  # 対応中なので残した（印を付け、画面で件数を知らせる）
    NOTED = "noted"  # 提出済み・完了などなのでメモを付けて残した


def revert_raised_task(
    filing: Filing, *, source: str, reason: str, by: User | None, by_record: bool = False
) -> Reverted:
    """元の事由（終了の登録など）を取り消したときの、起票済みのタスクの扱い（T28a〜T28c で共通）。

    - 未着手: 理由つきで取消
    - 対応中: 残す。trigger_details に元の事由の取消の印を付け、タスクに表示する（task_notes）
    - 提出済み・受理済・完了・不要: 残してメモを付ける
    """
    status = FilingStatus(filing.status)
    if status is FilingStatus.NOT_STARTED:
        transition(filing, FilingStatus.CANCELLED, by=by, reason=reason, by_record=by_record)
        return Reverted.CANCELLED
    with audit.event(AuditAction.FILING_TRANSITION, target=filing, actor=by):
        if status is FilingStatus.IN_PROGRESS:
            filing.trigger_details = dict(filing.trigger_details or {}) | {
                "source_cancelled": {"source": source, "reason": reason}
            }
        filing.note = f"{filing.note}\n{reason}".strip()
        filing.updated_by = by
        filing.save(update_fields=["trigger_details", "note", "updated_by", "updated_at"])
    return Reverted.LEFT_OPEN if status is FilingStatus.IN_PROGRESS else Reverted.NOTED


def on_termination_cancelled(
    contract: EmploymentContract, *, terminated_on: date, by: User | None, reason: str = ""
) -> list[Filing]:
    """終了の取り消し → 起票済みの終了届出・受入れ困難届出を共通の規則で戻す。

    対応中として残したタスクを返す（画面で件数を知らせる）。
    """
    note = (
        _("The termination of the employment contract was cancelled: %(reason)s")
        % {"reason": reason}
        if reason
        else _("The termination of the employment contract was cancelled.")
    )
    left: list[Filing] = []
    for kind in (FilingKind.CONTRACT_TERMINATION, FilingKind.ACCEPTANCE_DIFFICULTY):
        filing = find_open(kind, contract, terminated_on)
        if filing is None:
            continue
        outcome = revert_raised_task(filing, source="contract_termination", reason=note, by=by)
        if outcome is Reverted.LEFT_OPEN:
            left.append(filing)
    return left


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


@dataclass(frozen=True)
class DeadlineCandidate:
    """期限の到来で起票するタスクの元（日次の起票と、ダッシュボードの今後の予定で共通）。"""

    kind: FilingKind
    target: models.Model
    basis_on: date
    trigger: Trigger


def deadline_candidates() -> Iterator[DeadlineCandidate]:
    """在留期限・旅券・自機関の登録・加入の証明・支援委託契約・雇用契約の期間満了の期限。

    受付・着手の日にかかわらず、今ある期限をすべて返す（起票するかは呼び出す側が決める）。
    無効化した外国人の期限は返さない。
    """
    # 無効化した外国人は対象にしない（念のため create_if_active でも外す）
    for latest in LatestResidenceRecord.objects.select_related("record", "worker").filter(
        worker__status=WorkerStatus.RESIDING, worker__deactivated_at__isnull=True
    ):
        yield DeadlineCandidate(
            FilingKind.RESIDENCE_RENEWAL,
            latest.record,
            latest.record.expires_on,
            Trigger.RESIDENCE_EXPIRY,
        )

    for latest_passport in LatestPassportRecord.objects.select_related("record").filter(
        worker__status__in=[WorkerStatus.PRE_ENTRY, WorkerStatus.RESIDING],
        worker__deactivated_at__isnull=True,
    ):
        yield DeadlineCandidate(
            FilingKind.PASSPORT_CHECK,
            latest_passport.record,
            latest_passport.record.expires_on,
            Trigger.PASSPORT_EXPIRY,
        )

    organization = Organization.objects.first()
    if organization is not None:
        yield DeadlineCandidate(
            FilingKind.REGISTRATION_RENEWAL,
            organization,
            organization.registration_expires_on,
            Trigger.REGISTRATION_EXPIRY,
        )

    for membership in HostMembership.objects.filter(valid_until__isnull=False):
        assert membership.valid_until is not None
        yield DeadlineCandidate(
            FilingKind.MEMBERSHIP_RENEWAL,
            membership,
            membership.valid_until,
            Trigger.DOCUMENT_EXPIRY,
        )  # 業務画面の保存のときは sync_membership（同じ起票。二重にはならない）
    for attachment in _latest_attachments_with_expiry():
        assert attachment.valid_until is not None
        yield DeadlineCandidate(
            FilingKind.MEMBERSHIP_RENEWAL,
            attachment,
            attachment.valid_until,
            Trigger.DOCUMENT_EXPIRY,
        )

    for support_contract in SupportContract.objects.filter(
        ends_on__isnull=False, terminated_on__isnull=True
    ):
        assert support_contract.ends_on is not None
        if not _support_contract_renewed(support_contract):
            yield DeadlineCandidate(
                FilingKind.SUPPORT_CONTRACT_RENEWAL,
                support_contract,
                support_contract.ends_on,
                Trigger.SUPPORT_CONTRACT_END,
            )  # 業務画面の保存のときは sync_support_contracts

    for contract in EmploymentContract.objects.filter(
        terminated_on__isnull=True,
        current_ends_on__isnull=False,
        worker__deactivated_at__isnull=True,
    ):
        assert contract.current_ends_on is not None
        yield DeadlineCandidate(
            FilingKind.CONTRACT_EXPIRY_CHECK,
            contract,
            contract.current_ends_on,
            Trigger.CONTRACT_EXPIRY,
        )


@dataclass
class ScheduledResult:
    created: list[Filing]
    existing: int


def create_scheduled(today: date, *, by: User | None = None) -> ScheduledResult:
    """日付の到来で起票するもの。受付・着手の日を過ぎていて、まだ無いものだけ作る（冪等）。"""
    result = ScheduledResult(created=[], existing=0)

    def add(kind: FilingKind, target: models.Model, basis_on: date, trigger: Trigger) -> None:
        filing, created = _create_if_open(kind, target, basis_on, trigger, today=today, by=by)
        if filing is None:
            return
        if created:
            result.created.append(filing)
        else:
            result.existing += 1

    for candidate in deadline_candidates():
        add(candidate.kind, candidate.target, candidate.basis_on, candidate.trigger)
    organization = Organization.objects.first()

    for worker, basis_on in _workers_due_for_interview(today):
        add(FilingKind.PERIODIC_INTERVIEW, worker, basis_on, Trigger.INTERVIEW_CYCLE)

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

    # 署名済み書類の回収（生成から自機関の設定の日数。T33）
    from services import form_signatures  # 循環を避ける

    result.created.extend(form_signatures.raise_collection_tasks(today, by=by))
    return result


def _workers_due_for_interview(today: date) -> list[tuple[ForeignWorker, date]]:
    """定期面談のタスクが無い外国人と起算日（T19）。

    対象は在留中・無効化していない・今日を期間に含む雇用契約がある人。開いている定期面談のタスクが
    あれば作らない（起算日が変わっても重ねない）。起算日は前回の面談日（取消を除く。本人用・上司用の
    早い方）。前回の面談が今の雇用契約の開始より前（転籍の前）でも前回の面談日にする（自機関が続けて
    支援している人の「3か月に1回以上」は転籍で途切れない）。今の契約の開始日を使うのは、面談の記録が
    1 件も無いときだけ。記録のときに次回のタスクを作るので、ここは漏れの補い。
    """
    open_ids = set(
        Filing.objects.filter(
            kind=FilingKind.PERIODIC_INTERVIEW, status__in=list(OPEN_STATUSES)
        ).values_list("worker_id", flat=True)
    )
    result = []
    contracts = (
        EmploymentContract.objects.filter(
            worker__status=WorkerStatus.RESIDING,
            worker__deactivated_at__isnull=True,
            terminated_on__isnull=True,
        )
        .filter(models.Q(current_ends_on__isnull=True) | models.Q(current_ends_on__gte=today))
        .annotate(first_starts_on=models.Min("versions__starts_on"))
        .select_related("worker")
    )
    for contract in contracts:
        worker = contract.worker
        starts_on = contract.first_starts_on or contract.current_starts_on
        if starts_on > today or worker.pk in open_ids:
            continue
        last = (
            PeriodicInterview.objects.filter(worker=worker, voided_at__isnull=True)
            .order_by("-basis_on")
            .values_list("basis_on", flat=True)
            .first()
        )
        result.append((worker, last or starts_on))
        open_ids.add(worker.pk)
    return result


def _latest_attachments_with_expiry() -> list[Attachment]:
    """同じ対象・同じ種別の添付のうち最新のもの（差し替え前の古い証明書は見ない）。

    加入状況（HostMembership）の証明書は、加入状況の有効期限で起票するので対象外にする
    （同じ証明書のタスクを 2 件作らないため）。協力確認書の控えは期限が無いので対象外。
    """
    excluded_types = [
        ContentType.objects.get_for_model(HostMembership),
        # 協力確認書には有効期限が無い（運用要領 本体 印刷 80〜81頁。ADR-0015）
        ContentType.objects.get_for_model(HostCooperationConfirmation),
    ]
    latest: dict[tuple[int, int, str], Attachment] = {}
    queryset = Attachment.objects.filter(voided_at__isnull=True)  # 取り消した添付は見ない
    for attachment in queryset.exclude(content_type__in=excluded_types).order_by(
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


def _create_if_open(
    kind: FilingKind,
    target: models.Model,
    basis_on: date,
    trigger: Trigger,
    *,
    today: date,
    by: User | None,
) -> tuple[Filing | None, bool]:
    """受付・着手の日を過ぎていれば起票する（日次の起票と保存のときの起票で共通）。"""
    deadline = definition(kind).deadline(basis_on)
    if deadline.opens_on is not None and today < deadline.opens_on:
        return None, False
    return create_if_active(kind, target=target, basis_on=basis_on, trigger=trigger, by=by)


def _support_contract_renewed(contract: SupportContract) -> bool:
    """終了日より後に始まる同じ受入れ機関の契約がある（更新後の期間は次の行で持つ。ADR-0015）。"""
    if contract.ends_on is None:
        return False
    return SupportContract.objects.filter(
        host_organization_id=contract.host_organization_id, starts_on__gt=contract.ends_on
    ).exists()


# --- 業務画面の保存のときの起票と、古い期限のタスクの片付け（T28a）-------------------------------


@dataclass(frozen=True)
class SyncResult:
    """保存のときの期限のタスクの処理の結果。"""

    created: list[Filing]  # 新しく起票した（受付・着手の日を過ぎている期限だけ）
    closed: list[Filing]  # 自動で完了・取消にした
    left_open: list[Filing]  # 対応中のため動かさなかった（画面で知らせる）


def _unfinished_of(kind: FilingKind, target: models.Model) -> list[Filing]:
    return list(
        Filing.objects.filter(
            kind=kind,
            content_type=ContentType.objects.get_for_model(target),
            object_id=target.pk,
            status__in=list(OPEN_STATUSES),
        ).order_by("basis_on", "pk")
    )


def _close_automatically(
    filing: Filing, to: FilingStatus, *, by: User | None, reason: str, source: models.Model
) -> None:
    """自動の完了・取消。理由を残し、監査ログに自動である印と元の記録を入れる（人の操作と区別する）。"""
    transition(
        filing,
        to,
        by=by,
        reason=reason,
        context={
            "automatic": True,
            "source": f"{source._meta.label_lower}:{source.pk}",
            "reason": reason,
        },
    )


def _close_stale(
    kind: FilingKind,
    target: models.Model,
    current_on: date | None,
    *,
    by: User | None,
    reason: Callable[[date], str],
    out_closed: list[Filing],
    out_left: list[Filing],
) -> None:
    """今の期限（current_on）と違う日付の未完了のタスクを片付ける。

    - 期限が延びた（今の期限がタスクの日付より後）: 更新が済んだので完了にする
    - 期限が早まった・無くなった: 未着手なら取消。対応中は動かさない（人が確かめる）
    """
    for filing in _unfinished_of(kind, target):
        if filing.basis_on == current_on:
            continue
        if current_on is not None and current_on > filing.basis_on:
            _close_automatically(
                filing, FilingStatus.COMPLETED, by=by, reason=reason(filing.basis_on), source=target
            )
            out_closed.append(filing)
        elif filing.status == FilingStatus.NOT_STARTED:
            _close_automatically(
                filing, FilingStatus.CANCELLED, by=by, reason=reason(filing.basis_on), source=target
            )
            out_closed.append(filing)
        else:
            out_left.append(filing)


def _dates(old: date, new: date | None, *, none: str) -> dict[str, str]:
    return {"old": old.isoformat(), "new": new.isoformat() if new else none}


@transaction.atomic
def sync_membership(
    membership: HostMembership,
    *,
    by: User | None,
    today: date,
    certificate_replaced: bool = False,
) -> SyncResult:
    """加入状況の有効期限に合わせて、更新のタスクを片付けて起票する（日次の起票と同じタスク）。"""
    closed: list[Filing] = []
    left: list[Filing] = []
    new = membership.valid_until

    def reason(old: date) -> str:
        dates = _dates(old, new, none=_("no expiry date"))
        if certificate_replaced:
            return (
                _(
                    "The expiry date was updated by replacing the membership certificate "
                    "(%(old)s → %(new)s)."
                )
                % dates
            )
        return _("The expiry date of the membership was changed (%(old)s → %(new)s).") % dates

    _close_stale(
        FilingKind.MEMBERSHIP_RENEWAL,
        membership,
        new,
        by=by,
        reason=reason,
        out_closed=closed,
        out_left=left,
    )
    created: list[Filing] = []
    if new is not None:
        filing, was_created = _create_if_open(
            FilingKind.MEMBERSHIP_RENEWAL,
            membership,
            new,
            Trigger.DOCUMENT_EXPIRY,
            today=today,
            by=by,
        )
        if filing is not None and was_created:
            created.append(filing)
    return SyncResult(created=created, closed=closed, left_open=left)


@transaction.atomic
def sync_support_contracts(host: HostOrganization, *, by: User | None, today: date) -> SyncResult:
    """受入れ機関の支援委託契約の終了日に合わせて、更新のタスクを片付けて起票する。

    次の契約（終了日より後に始まる契約）があれば、その契約の更新のタスクは完了にする。
    """
    closed: list[Filing] = []
    left: list[Filing] = []
    created: list[Filing] = []
    for contract in host.support_contracts.order_by("starts_on", "pk"):
        if contract.terminated_on is not None:
            # 終了の登録で更新は不要になった（ADR-0017。次の行か終了の登録まで完了にしない）
            for filing in _unfinished_of(FilingKind.SUPPORT_CONTRACT_RENEWAL, contract):
                _close_automatically(
                    filing,
                    FilingStatus.COMPLETED,
                    by=by,
                    reason=_("The termination of the support contract was registered (%(day)s).")
                    % {"day": contract.terminated_on.isoformat()},
                    source=contract,
                )
                closed.append(filing)
            continue
        following = (
            SupportContract.objects.filter(
                host_organization_id=contract.host_organization_id,
                starts_on__gt=contract.ends_on,
            )
            .order_by("starts_on")
            .first()
            if contract.ends_on is not None
            else None
        )
        if following is not None:
            for filing in _unfinished_of(FilingKind.SUPPORT_CONTRACT_RENEWAL, contract):
                _close_automatically(
                    filing,
                    FilingStatus.COMPLETED,
                    by=by,
                    reason=_("The next support contract (from %(start)s) was registered.")
                    % {"start": following.starts_on.isoformat()},
                    source=following,
                )
                closed.append(filing)
            continue

        ends_on = contract.ends_on

        def reason(old: date, ends_on: date | None = ends_on) -> str:
            return _(
                "The end date of the support contract was changed (%(old)s → %(new)s)."
            ) % _dates(old, ends_on, none=_("no end date"))

        _close_stale(
            FilingKind.SUPPORT_CONTRACT_RENEWAL,
            contract,
            ends_on,
            by=by,
            reason=reason,
            out_closed=closed,
            out_left=left,
        )
        if ends_on is not None:
            raised, was_created = _create_if_open(
                FilingKind.SUPPORT_CONTRACT_RENEWAL,
                contract,
                ends_on,
                Trigger.SUPPORT_CONTRACT_END,
                today=today,
                by=by,
            )
            if raised is not None and was_created:
                created.append(raised)
    return SyncResult(created=created, closed=closed, left_open=left)


# --- タスクの注意書き（画面の表示）---------------------------------------------------------


def task_notes(filing: Filing, today: date) -> list[str]:
    """タスクに添えて表示する注意（ダッシュボードの行とタスクの詳細）。"""
    notes: list[str] = []
    kind = FilingKind(filing.kind)
    open_ = FilingStatus(filing.status) in OPEN_STATUSES
    if kind is FilingKind.SUPPORT_CONTRACT_RENEWAL and open_ and filing.basis_on < today:
        notes.append(_("The end date has passed: register the next period or the termination."))
    cancelled = (filing.trigger_details or {}).get("source_cancelled")
    if cancelled and open_:
        label = SOURCE_CANCELLED_LABELS.get(cancelled.get("source", ""), "")
        notes.append(
            _("The original event (%(event)s) was cancelled. Check this task.") % {"event": label}
        )
    if kind is FilingKind.SIGNED_COPY_COLLECTION and open_:
        notes.append(
            _(
                "Attach the signed copy on the Forms tab of the foreign worker; this task is then "
                "completed automatically."
            )
        )
    notes.extend(_support_contract_notes(filing))
    if kind is FilingKind.CONTRACT_TERMINATION and (filing.trigger_details or {}).get(
        "support_contract"
    ):
        notes.append(
            _(
                "A support contract was in effect: fill in column c (termination of the "
                "support contract) of form 3-1-2. Form 3-3-2 is then not required."
            )
        )
    return notes


def _support_contract_notes(filing: Filing) -> list[str]:
    """支援委託契約の届出タスク（第3-3-1号・3-3-2号と、それに伴う第3-2号）に、記入の手がかりを添える。"""
    details = filing.trigger_details or {}
    if not details.get("support_contract"):
        return []
    kind = FilingKind(filing.kind)
    event = details.get("event")
    if kind is FilingKind.SUPPORT_PLAN_CHANGE:
        return [
            _(
                "The support plan changes because the support contract was %(event)s "
                "(table %(item)s). Submit it before form 3-3-2."
            )
            % {
                "event": _("concluded") if event == "concluded" else _("terminated"),
                "item": {"III-6": "Ⅲ⑥", "III-7": "Ⅲ⑦"}.get(details.get("plan_change_item", ""), ""),
            }
        ]
    if kind is FilingKind.SUPPORT_CONTRACT_END_OR_CONCLUSION and event == "concluded":
        return [
            _(
                "Conclusion of the support contract on %(day)s: fill in B (conclusion) of "
                "form 3-3-2 and attach form 1-25."
            )
            % {"day": details.get("concluded_on", "")}
        ]
    if kind is FilingKind.SUPPORT_CONTRACT_END_OR_CONCLUSION and event == "terminated":
        category = details.get("termination_category") or ""
        reason = details.get("termination_reason") or ""
        reason_label = (
            str(SupportTerminationReason(reason).label)
            if reason in SupportTerminationReason.values
            else ""
        )
        if details.get("termination_note"):
            reason_label = f"{reason_label}（{details['termination_note']}）"
        return [
            _(
                "Termination of the support contract on %(day)s (%(category)s / %(reason)s): "
                "fill in A (termination) of form 3-3-2."
            )
            % {
                "day": details.get("terminated_on", ""),
                "category": (
                    str(SupportTerminationCategory(category).label)
                    if category in SupportTerminationCategory.values
                    else ""
                ),
                "reason": reason_label,
            }
        ]
    if kind is FilingKind.SUPPORT_CONTRACT_CHANGE:
        changes = []
        for name, (before, after) in (details.get("changes") or {}).items():
            label = str(getattr(SupportContract._meta.get_field(name), "verbose_name", name))
            changes.append(f"{label}: {before or '—'} → {after or '—'}")
        return [
            _("Change of the support contract on %(day)s: %(changes)s")
            % {"day": details.get("changed_on", ""), "changes": "、".join(changes)}
        ]
    return []


# --- 状態の遷移・起算日の訂正 --------------------------------------------------------------


def transition(
    filing: Filing,
    to: FilingStatus,
    *,
    by: User | None,
    submitted_on: date | None = None,
    receipt_number: str = "",
    reason: str = "",
    by_record: bool = False,
    context: dict[str, Any] | None = None,
) -> None:
    """状態を変える。許されない遷移・必須の項目の不足は ValidationError。監査ログに 1 件残す。

    by_record は定期面談の記録（とその取消）から呼ぶときだけ True にする。
    context は監査ログに足す情報（自動の完了・取消の印と元の記録。sync_* が渡す）。
    """
    if to == FilingStatus(filing.status):
        return
    validate_transition(
        filing,
        to,
        submitted_on=submitted_on,
        receipt_number=receipt_number,
        reason=reason,
        by_record=by_record,
    )
    with audit.event(AuditAction.FILING_TRANSITION, target=filing, actor=by, context=context):
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
    by_record: bool = False,
) -> None:
    current = FilingStatus(filing.status)
    if to not in allowed_transitions(filing, by_record=by_record):
        raise ValidationError(
            _("The status cannot be changed from %(from)s to %(to)s.")
            % {"from": current.label, "to": to.label},
            code="transition",
        )
    if to is FilingStatus.SUBMITTED and (submitted_on or filing.submitted_on) is None:
        raise ValidationError(_("Enter the submission date."), code="submitted_on")
    if to is FilingStatus.ACCEPTED and not (receipt_number or filing.receipt_number).strip():
        raise ValidationError(_("Enter the receipt number."), code="receipt_number")
    reopening = current is FilingStatus.COMPLETED
    if (to in (FilingStatus.NOT_REQUIRED, FilingStatus.CANCELLED) or reopening) and not (
        reason.strip()
    ):
        raise ValidationError(_("Enter the reason."), code="reason")
    if (
        to in OPEN_STATUSES
        and current not in OPEN_STATUSES
        and _other_unfinished_exists(filing, filing.basis_on)
    ):
        raise ValidationError(
            _("Another unfinished task of the same kind exists for the same date."),
            code="unfinished_exists",
        )


def _scheduled_on(filing: Filing) -> date | None:
    if FilingKind(filing.kind) is not FilingKind.PERIODIC_INTERVIEW:
        return None
    value = (filing.trigger_details or {}).get("scheduled_on")
    return date.fromisoformat(value) if value else None


def validate_rebase(filing: Filing, basis_on: date) -> None:
    """起算日を直せるか（未完了のタスクだけ。直した日付に同じ種別の未完了のタスクがあれば不可）。"""
    if FilingStatus(filing.status) not in OPEN_STATUSES:
        raise ValidationError(_("Only open tasks can be changed."), code="closed")
    if _other_unfinished_exists(filing, basis_on):
        raise ValidationError(
            _("Another unfinished task of the same kind exists for the same date."),
            code="unfinished_exists",
        )


def rebase(filing: Filing, basis_on: date, *, by: User | None, reason: str) -> None:
    """起算日（事由の日）を直して期限を計算し直す（例: 受入れ困難の事由が終了日より前）。"""
    if not reason.strip():
        raise ValidationError(_("Enter the reason."), code="reason")
    validate_rebase(filing, basis_on)
    deadline = definition(filing.kind).deadline(basis_on)
    scheduled = _scheduled_on(filing)
    if scheduled is not None and basis_on < scheduled <= deadline.due_on:
        # 定期面談の次回の予定日は、新しい法定の期限の範囲内なら保つ
        deadline = deadlines.periodic_interview(basis_on, scheduled)
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


# --- 記録を通した完了・取り消し（署名済み書類の回収。T33）------------------------------------


def _automatic(source: models.Model, reason: str) -> dict[str, Any]:
    return {
        "automatic": True,
        "source": f"{source._meta.label_lower}:{source.pk}",
        "reason": reason,
    }


def complete_by_record(
    filing: Filing, *, by: User | None, reason: str, source: models.Model
) -> None:
    """記録（署名済みの添付）で自動で完了する。"""
    transition(
        filing,
        FilingStatus.COMPLETED,
        by=by,
        reason=reason,
        by_record=True,
        context=_automatic(source, reason),
    )


def reopen_by_record(filing: Filing, *, by: User | None, reason: str, source: models.Model) -> None:
    """記録の取消（署名済みの添付の取消）で、自動で完了したタスクを未着手に戻す。"""
    transition(
        filing,
        FilingStatus.NOT_STARTED,
        by=by,
        reason=reason,
        by_record=True,
        context=_automatic(source, reason),
    )


def close_not_required(
    filing: Filing, *, by: User | None, reason: str, source: models.Model
) -> None:
    """自動で「不要」にする（新しい生成物ができた古い生成物の回収など）。"""
    transition(
        filing, FilingStatus.NOT_REQUIRED, by=by, reason=reason, context=_automatic(source, reason)
    )


# --- 生成した帳票への参照（T33-D6、ADR-0022）---------------------------------------------


def linkable_documents(filing: Filing) -> models.QuerySet[Any]:
    """紐づけられる生成物（同じ外国人の、生成が完了したもの。紐づけ済みを除く）。"""
    from apps.forms_engine.models import GeneratedDocument, GenerationStatus  # 循環を避ける

    if filing.worker_id is None:
        return GeneratedDocument.objects.none()
    return (
        GeneratedDocument.objects.filter(
            worker_id=filing.worker_id, status=GenerationStatus.COMPLETED
        )
        .exclude(filings=filing)
        .select_related("template")
        .order_by("-generated_at", "-pk")
    )


def link_document(filing: Filing, document: Any, *, by: User | None) -> None:
    """届出タスクに生成物を紐づける（同じ外国人の完了した生成物だけ。監査ログに残る）。"""
    if not linkable_documents(filing).filter(pk=document.pk).exists():
        raise ValidationError(
            _("Only completed documents of the same foreign worker can be linked."),
            code="document",
        )
    filing.generated_documents.add(document)


def unlink_document(filing: Filing, document: Any, *, by: User | None) -> None:
    filing.generated_documents.remove(document)
