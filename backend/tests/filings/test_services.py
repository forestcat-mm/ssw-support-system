"""届出タスクの起票・取り消し・日次の起票・状態の遷移・起算日の訂正。"""

from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.utils import translation

from apps.contracts.models import EmploymentContractAllowance, TerminationReason
from apps.core.models import AuditAction, AuditLog, Organization
from apps.filings.models import Filing, FilingKind, FilingStatus, Trigger
from apps.hosts.models import HostMembership, SupportContract
from apps.workers.models import WorkerStatus
from services import contracts, filings
from tests.contracts.factories import add_version, make_contract
from tests.core.factories import make_organization, make_staff, make_user
from tests.hosts.factories import VALID_NUMBERS, field, make_host, make_site
from tests.workers.factories import add_passport, add_residence, make_worker

pytestmark = pytest.mark.django_db


def _kinds() -> list[str]:
    return sorted(
        Filing.objects.exclude(status=FilingStatus.CANCELLED).values_list("kind", flat=True)
    )


# --- 契約の終了 ------------------------------------------------------------------


def test_termination_creates_14_day_filing_rolled_over_holidays() -> None:
    """T11 の完了条件: 契約終了を登録すると 14 日期限の Filing が自動生成。"""
    staff = make_staff()
    contract = make_contract(make_worker(assigned_staff=staff))
    contracts.terminate(contract, on=date(2026, 12, 28), reason=TerminationReason.EXPIRY, by=None)
    [filing] = Filing.objects.all()
    assert filing.kind == FilingKind.CONTRACT_TERMINATION
    assert filing.target == contract
    assert filing.worker == contract.worker
    assert filing.assignee == staff
    assert (filing.basis_on, filing.unadjusted_due_on, filing.due_on) == (
        date(2026, 12, 28),
        date(2027, 1, 11),
        date(2027, 1, 12),
    )
    assert filing.trigger == Trigger.CONTRACT_TERMINATED
    assert filing.trigger_details["termination_reason"] == "expiry"
    assert filings.definition(filing.kind).filer is filings.Filer.HOST


@pytest.mark.parametrize(
    ("reason", "with_difficulty"),
    [
        (TerminationReason.EXPIRY, False),
        (TerminationReason.VOLUNTARY, False),
        (TerminationReason.JOB_CHANGE, False),
        (TerminationReason.COMPANY, True),
        (TerminationReason.OTHER, True),
    ],
)
def test_acceptance_difficulty_depends_on_reason(reason: str, with_difficulty: bool) -> None:
    contract = make_contract()
    contracts.terminate(contract, on=date(2026, 9, 30), reason=reason, note="メモ", by=None)
    expected = (
        ["acceptance_difficulty", "contract_termination"]
        if with_difficulty
        else ["contract_termination"]
    )
    assert _kinds() == expected


def test_cancelling_termination_cancels_filings_with_reason() -> None:
    contract = make_contract()
    contracts.terminate(contract, on=date(2026, 9, 30), reason=TerminationReason.COMPANY, by=None)
    contracts.cancel_termination(contract, by=None)
    assert set(Filing.objects.values_list("status", flat=True)) == {FilingStatus.CANCELLED}
    assert all("取り消されました" in f.status_reason for f in Filing.objects.all())
    # 取り消した後にもう一度終了を登録すると、新しく起票される
    contracts.terminate(contract, on=date(2026, 9, 30), reason=TerminationReason.EXPIRY, by=None)
    assert _kinds() == ["contract_termination"]
    assert Filing.objects.count() == 3


def test_submitted_filing_is_not_cancelled_but_noted() -> None:
    contract = make_contract()
    contracts.terminate(contract, on=date(2026, 9, 30), reason=TerminationReason.EXPIRY, by=None)
    filing = Filing.objects.get()
    filings.transition(filing, FilingStatus.SUBMITTED, by=None, submitted_on=date(2026, 10, 5))
    contracts.cancel_termination(contract, by=None)
    filing.refresh_from_db()
    assert filing.status == FilingStatus.SUBMITTED
    assert "取り消されました" in filing.note


# --- 版の追加・新たな契約 ---------------------------------------------------------------


def test_wage_decrease_creates_change_filing_with_findings() -> None:
    contract = make_contract()
    version = add_version(contract, base_wage=200000, changed_on=date(2026, 10, 1))
    EmploymentContractAllowance.objects.create(version=version, name="住宅手当", amount=10000)
    contracts.complete_version(version, by=None)
    filing = Filing.objects.get(kind=FilingKind.CONTRACT_CHANGE)
    assert filing.basis_on == date(2026, 10, 1)
    assert filing.due_on == date(2026, 10, 15)
    assert [f["code"] for f in filing.trigger_details["required"]] == ["base_wage_decreased"]
    assert [f["code"] for f in filing.trigger_details["review"]] == ["deduction_removed"]
    assert filing.needs_review is False  # 届出が必要な変更があるので確認の印は付けない


def test_raise_only_creates_no_filing() -> None:
    contract = make_contract()
    version = add_version(contract, base_wage=230000, changed_on=date(2026, 10, 1))
    EmploymentContractAllowance.objects.create(version=version, name="住宅手当", amount=10000)
    from apps.contracts.models import EmploymentContractDeduction

    EmploymentContractDeduction.objects.create(version=version, kind="housing", amount=30000)
    contracts.complete_version(version, by=None)
    assert not Filing.objects.exists()


def test_review_only_change_is_flagged() -> None:
    contract = make_contract()
    version = add_version(contract, duties="機械加工と検査", changed_on=date(2026, 10, 1))
    EmploymentContractAllowance.objects.create(version=version, name="住宅手当", amount=10000)
    from apps.contracts.models import EmploymentContractDeduction

    EmploymentContractDeduction.objects.create(version=version, kind="housing", amount=30000)
    contracts.complete_version(version, by=None)
    filing = Filing.objects.get()
    assert filing.needs_review is True
    assert [f["code"] for f in filing.trigger_details["review"]] == ["duties_changed"]


def test_rehire_with_same_host_creates_new_contract_filing() -> None:
    worker = make_worker()
    first = make_contract(worker, starts_on=date(2025, 4, 1), ends_on=None)
    site = first.versions.get().site
    contracts.terminate(first, on=date(2026, 3, 31), reason=TerminationReason.VOLUNTARY, by=None)
    second = make_contract(
        worker, site=site, starts_on=date(2026, 5, 1), ends_on=None, signed_on=date(2026, 4, 20)
    )
    contracts.complete_version(second.versions.get(), by=None)
    filing = Filing.objects.get(kind=FilingKind.CONTRACT_NEW)
    assert filing.basis_on == date(2026, 4, 20)

    other_worker = make_worker(name_romaji="LI NA")
    other_site = make_site(make_host(name="別会社", corporate_number=VALID_NUMBERS[1]))
    third = make_contract(other_worker, site=other_site)
    contracts.complete_version(third.versions.get(), by=None)
    assert (
        Filing.objects.filter(kind=FilingKind.CONTRACT_NEW).count() == 1
    )  # 初めての契約は起票しない


# --- 日次の起票 ------------------------------------------------------------------


def test_scheduled_filings_are_created_on_opening_dates_and_idempotent() -> None:
    worker = make_worker(status=WorkerStatus.RESIDING)
    add_residence(worker, expires_on=date(2027, 1, 15))  # 受付開始 2026-10-15
    add_passport(worker, expires_on=date(2027, 4, 15))  # 着手 2026-10-15
    make_organization(registration_expires_on=date(2027, 4, 30))  # 受付開始 2026-10-01
    host = make_host()
    HostMembership.objects.create(
        host_organization=host,
        field=field("food_manufacturing"),
        joined_on=date(2024, 4, 1),
        valid_until=date(2026, 11, 10),
    )
    SupportContract.objects.create(
        host_organization=host,
        concluded_on=date(2025, 3, 1),
        starts_on=date(2025, 4, 1),
        ends_on=date(2027, 1, 10),
    )
    make_contract(
        make_worker(name_romaji="LI NA"), site=make_site(host), ends_on=date(2026, 11, 15)
    )

    assert filings.create_scheduled(date(2026, 9, 30)).created == []
    result = filings.create_scheduled(date(2026, 10, 16))
    assert sorted(f.kind for f in result.created) == sorted(
        [
            "residence_renewal",
            "passport_check",
            "registration_renewal",
            "membership_renewal",
            "support_contract_renewal",
            "contract_expiry_check",
        ]
    )
    again = filings.create_scheduled(date(2026, 10, 16))
    assert again.created == [] and again.existing == 6
    renewal = Filing.objects.get(kind=FilingKind.REGISTRATION_RENEWAL)
    assert renewal.final_on == date(2027, 1, 31)
    assert filings.definition(renewal.kind).filer is filings.Filer.SUPPORT_ORGANIZATION


def test_residence_renewal_only_for_residing_workers() -> None:
    add_residence(make_worker(status=WorkerStatus.RETURNED), expires_on=date(2027, 1, 15))
    assert filings.create_scheduled(date(2026, 12, 1)).created == []


def test_periodic_filings_for_hosts_with_active_contract_and_own_organization() -> None:
    make_organization(registration_expires_on=date(2031, 3, 31))
    active = make_host()
    SupportContract.objects.create(
        host_organization=active, concluded_on=date(2026, 3, 1), starts_on=date(2026, 3, 15)
    )
    ended = make_host(name="契約終了", corporate_number=VALID_NUMBERS[1])
    SupportContract.objects.create(
        host_organization=ended,
        concluded_on=date(2025, 1, 1),
        starts_on=date(2025, 1, 1),
        ends_on=date(2026, 1, 31),
    )
    assert not [
        f
        for f in filings.create_scheduled(date(2026, 3, 31)).created
        if f.kind.startswith("periodic")
    ]
    created = filings.create_scheduled(date(2026, 4, 1)).created
    periodic = {(f.kind, f.target_repr) for f in created if f.kind.startswith("periodic")}
    assert periodic == {
        ("periodic_host", str(active)),
        ("periodic_support_organization", str(Organization.objects.get())),
    }
    assert {f.due_on for f in created if f.kind.startswith("periodic")} == {date(2026, 5, 31)}


def test_management_command_with_date() -> None:
    from io import StringIO

    make_organization(registration_expires_on=date(2027, 4, 30))
    out = StringIO()
    call_command("create_scheduled_filings", "--date", "2026-10-16", stdout=out)
    assert "created 1" in out.getvalue()
    call_command("create_scheduled_filings", "--date", "2026-10-16", stdout=out)
    assert "created 0, already existed 1" in out.getvalue()


# --- 状態の遷移・起算日の訂正 --------------------------------------------------------


def _filing() -> Filing:
    contract = make_contract()
    contracts.terminate(contract, on=date(2026, 9, 30), reason=TerminationReason.EXPIRY, by=None)
    return Filing.objects.get()


def test_transitions_and_required_fields() -> None:
    user = make_user("tanto")
    filing = _filing()
    with pytest.raises(ValidationError, match="提出日"):
        filings.transition(filing, FilingStatus.SUBMITTED, by=user)
    filings.transition(filing, FilingStatus.IN_PROGRESS, by=user)
    filings.transition(filing, FilingStatus.SUBMITTED, by=user, submitted_on=date(2026, 10, 5))
    with pytest.raises(ValidationError, match="受理番号"):
        filings.transition(filing, FilingStatus.ACCEPTED, by=user)
    filings.transition(filing, FilingStatus.ACCEPTED, by=user, receipt_number="R-0001")
    with pytest.raises(ValidationError, match="変更することはできません"):
        filings.transition(filing, FilingStatus.IN_PROGRESS, by=user)
    log = AuditLog.objects.filter(action=AuditAction.FILING_TRANSITION).latest("pk")
    assert log.actor == user
    assert log.changes["status"] == ["submitted", "accepted"]
    assert log.changes["receipt_number"] == ["", "R-0001"]


def test_not_required_needs_reason_and_can_be_reopened() -> None:
    filing = _filing()
    with pytest.raises(ValidationError, match="理由"):
        filings.transition(filing, FilingStatus.NOT_REQUIRED, by=None)
    filings.transition(filing, FilingStatus.NOT_REQUIRED, by=None, reason="管轄局に確認済み")
    filings.transition(filing, FilingStatus.NOT_STARTED, by=None)
    assert Filing.objects.get(pk=filing.pk).status == FilingStatus.NOT_STARTED


def test_rebase_recalculates_deadline() -> None:
    contract = make_contract()
    contracts.terminate(contract, on=date(2026, 9, 30), reason=TerminationReason.COMPANY, by=None)
    difficulty = Filing.objects.get(kind=FilingKind.ACCEPTANCE_DIFFICULTY)
    filings.rebase(difficulty, date(2026, 8, 31), by=None, reason="解雇予告日")
    difficulty.refresh_from_db()
    assert (difficulty.basis_on, difficulty.due_on) == (date(2026, 8, 31), date(2026, 9, 14))
    log = AuditLog.objects.filter(action=AuditAction.FILING_TRANSITION).latest("pk")
    assert log.changes["basis_on"] == ["2026-09-30", "2026-08-31"]
    assert log.context == {"reason": "解雇予告日"}


def test_deadline_of_round_trips() -> None:
    filing = _filing()
    deadline = filings.deadline_of(filing)
    assert (deadline.due_on, deadline.unadjusted_due_on, deadline.basis_on) == (
        filing.due_on,
        filing.unadjusted_due_on,
        filing.basis_on,
    )


def test_every_kind_has_a_definition() -> None:
    assert set(filings.KIND_DEFINITIONS) == set(FilingKind)
    assert (
        filings.definition(FilingKind.SUPPORT_DIFFICULTY).filer
        is filings.Filer.SUPPORT_ORGANIZATION
    )
    assert filings.definition(FilingKind.PERIODIC_HOST).filer is filings.Filer.HOST


def test_support_difficulty_is_the_report_of_the_support_organization() -> None:
    """登録支援機関が出すのは特異事案報告（第4-3号）。所属機関の届出（第3-7号）ではない。"""
    with translation.override("ja"):
        label = str(FilingKind.SUPPORT_DIFFICULTY.label)
    assert "第4-3号" in label and "第3-7号" not in label


def test_periodic_filing_is_created_only_during_the_filing_period() -> None:
    make_organization(registration_expires_on=date(2031, 3, 31))
    assert not [
        f
        for f in filings.create_scheduled(date(2026, 6, 1)).created
        if f.kind.startswith("periodic")
    ]
    assert [f.kind for f in filings.create_scheduled(date(2026, 5, 31)).created] == [
        "periodic_support_organization"
    ]


def test_renewed_support_contract_is_not_flagged() -> None:
    host = make_host()
    SupportContract.objects.create(
        host_organization=host,
        concluded_on=date(2025, 3, 1),
        starts_on=date(2025, 4, 1),
        ends_on=date(2026, 3, 31),
    )
    SupportContract.objects.create(
        host_organization=host, concluded_on=date(2026, 3, 1), starts_on=date(2026, 4, 1)
    )
    assert (
        not Filing.objects.exists() and filings.create_scheduled(date(2026, 10, 16)).created == []
    )


def test_membership_certificate_is_checked_once_via_membership() -> None:
    from django.core.files.base import ContentFile

    from apps.core.models import AttachmentKind
    from services.attachments import create_attachment
    from tests.conftest import PDF_BYTES

    membership = HostMembership.objects.create(
        host_organization=make_host(),
        field=field("food_manufacturing"),
        joined_on=date(2024, 4, 1),
        valid_until=date(2026, 11, 10),
    )
    create_attachment(
        target=membership,
        kind=AttachmentKind.MEMBERSHIP_CERTIFICATE,
        file=ContentFile(PDF_BYTES, name="c.pdf"),
        created_by=None,
        valid_until=date(2026, 11, 10),
    )
    created = filings.create_scheduled(date(2026, 10, 16)).created
    assert [(f.kind, f.target) for f in created] == [("membership_renewal", membership)]


def test_only_latest_certificate_is_checked() -> None:
    """有効期限の欄が行に無い添付（自機関の協議会の証明書など）は、最新の添付だけを見る。"""
    from django.core.files.base import ContentFile

    from apps.core.models import AttachmentKind
    from services.attachments import create_attachment
    from tests.conftest import PDF_BYTES

    organization = make_organization(registration_expires_on=date(2031, 3, 31))
    for valid_until in (date(2026, 3, 31), date(2027, 3, 31)):
        create_attachment(
            target=organization,
            kind=AttachmentKind.FOOD_COUNCIL_CERTIFICATE,
            file=ContentFile(PDF_BYTES, name="c.pdf"),
            created_by=None,
            valid_until=valid_until,
        )
    created = filings.create_scheduled(date(2027, 3, 1)).created
    assert [f.basis_on for f in created if f.kind == "membership_renewal"] == [date(2027, 3, 31)]


def test_unfinished_tasks_are_unique_but_closed_ones_are_reused_by_default() -> None:
    """一意は未完了だけ（ADR-0012）。ほかの種別の起票は閉じたタスクを再利用し、重複を作らない。"""
    worker = make_worker()
    basis = date(2026, 10, 1)
    first, created = filings.create(
        FilingKind.CONTRACT_CHANGE, target=worker, basis_on=basis, trigger=Trigger.MANUAL
    )
    assert created
    filings.transition(first, FilingStatus.SUBMITTED, by=None, submitted_on=basis)
    again, created = filings.create(
        FilingKind.CONTRACT_CHANGE, target=worker, basis_on=basis, trigger=Trigger.MANUAL
    )
    assert (again, created) == (
        first,
        False,
    )  # 既定では閉じたタスクを返す（日次の起票の重複を防ぐ）
    other, created = filings.create(
        FilingKind.CONTRACT_CHANGE,
        target=worker,
        basis_on=basis,
        trigger=Trigger.MANUAL,
        reuse_closed=False,
    )
    assert created and other != first
    with pytest.raises(IntegrityError), transaction.atomic():
        Filing.objects.create(
            kind=FilingKind.CONTRACT_CHANGE,
            content_type=other.content_type,
            object_id=other.object_id,
            target_repr="x",
            worker=worker,
            trigger=Trigger.MANUAL,
            deadline_kind=other.deadline_kind,
            basis_on=basis,
            due_on=other.due_on,
            unadjusted_due_on=other.unadjusted_due_on,
        )  # 未完了の 2 件目は DB が拒否する
    # 起算日を直して、同じ日付の未完了のタスクと重なるときも拒否する
    moved, _created = filings.create(
        FilingKind.CONTRACT_CHANGE, target=worker, basis_on=date(2026, 9, 1), trigger=Trigger.MANUAL
    )
    with pytest.raises(ValidationError) as error:
        filings.rebase(moved, basis, by=None, reason="日付の誤り")
    assert error.value.code == "unfinished_exists"
