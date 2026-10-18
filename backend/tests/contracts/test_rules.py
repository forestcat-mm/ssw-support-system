"""雇用契約の業務ルール（状態・期間の重なり・派遣の不可・業務区分・差分・金額・同等報酬の根拠）。"""

from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction

from apps.contracts.models import (
    EmploymentContractAllowance,
    EmploymentContractVersion,
    ImmutableRowError,
    TerminationReason,
)
from apps.core.models import AttachmentKind
from services import contracts
from services.attachments import create_attachment
from tests.conftest import PDF_BYTES
from tests.hosts.factories import VALID_NUMBERS, category, make_host, make_site
from tests.workers.factories import make_worker

from .factories import add_version, make_contract, site_with_categories

pytestmark = pytest.mark.django_db


def test_status_of() -> None:
    contract = make_contract(starts_on=date(2026, 4, 1))
    assert contracts.status_of(contract, date(2026, 3, 31)) is contracts.ContractStatus.SCHEDULED
    assert contracts.status_of(contract, date(2026, 4, 1)) is contracts.ContractStatus.ACTIVE
    contracts.terminate(contract, on=date(2026, 9, 30), reason=TerminationReason.VOLUNTARY, by=None)
    assert contracts.status_of(contract, date(2026, 5, 1)) is contracts.ContractStatus.TERMINATED


def test_first_version_number_and_header_period() -> None:
    contract = make_contract(starts_on=date(2026, 4, 1), ends_on=date(2027, 3, 31))
    version = contract.versions.get()
    assert version.version == 1
    assert version.changed_on == date(2026, 4, 1)
    assert (contract.current_starts_on, contract.current_ends_on) == (
        date(2026, 4, 1),
        date(2027, 3, 31),
    )

    renewed = add_version(
        contract, starts_on=date(2026, 4, 1), ends_on=date(2028, 3, 31), changed_on=date(2027, 4, 1)
    )
    contract.refresh_from_db()
    assert renewed.version == 2
    assert contract.current_ends_on == date(2028, 3, 31)


def test_versions_and_details_are_immutable() -> None:
    contract = make_contract()
    version = contract.versions.get()
    version.base_wage = 1
    with pytest.raises(ImmutableRowError):
        version.save()
    with pytest.raises(ImmutableRowError):
        version.delete()
    allowance = version.allowances.get()
    allowance.amount = 1
    with pytest.raises(ImmutableRowError):
        allowance.save()
    assert EmploymentContractVersion.objects.get(pk=version.pk).base_wage == 220000


def test_diff_between_versions() -> None:
    contract = make_contract()
    v1 = contract.versions.get()
    v2 = add_version(contract, base_wage=230000, changed_on=date(2027, 4, 1))
    EmploymentContractAllowance.objects.create(version=v2, name="住宅手当", amount=12000)
    changes = contracts.diff(v1, v2)
    assert changes["base_wage"] == [220000, 230000]
    assert changes["allowances"] == [
        [("住宅手当", 10000, "", None)],
        [("住宅手当", 12000, "", None)],
    ]
    assert changes["deductions"] == [[("housing", "", 30000)], []]
    assert "change_reason" not in changes and "changed_on" not in changes
    assert contracts.previous_version(v2) == v1


def test_monthly_total_and_take_home() -> None:
    version = make_contract().versions.get()
    assert contracts.monthly_total(version) == 230000
    assert contracts.take_home(version) == 200000
    hourly = make_contract(
        site=make_site(make_host(name="B", corporate_number=VALID_NUMBERS[1])),
        wage_type="hourly",
        base_wage=1200,
    ).versions.get()
    assert contracts.monthly_total(hourly) is None  # 時給で月額の目安が無い


# --- 期間の重なり ----------------------------------------------------------------


def test_overlapping_contracts_of_same_worker_are_rejected() -> None:
    worker = make_worker()
    make_contract(worker, starts_on=date(2026, 4, 1), ends_on=date(2027, 3, 31))
    with pytest.raises(ValidationError, match="ID:"):
        contracts.validate_no_overlap(worker.pk, date(2027, 3, 31), None)
    contracts.validate_no_overlap(worker.pk, date(2027, 4, 1), None)
    contracts.validate_no_overlap(make_worker(name_romaji="LI NA").pk, date(2026, 4, 1), None)
    with pytest.raises(IntegrityError), transaction.atomic():
        make_contract(worker, starts_on=date(2026, 10, 1), ends_on=None)


def test_contract_after_termination_is_allowed() -> None:
    worker = make_worker()
    first = make_contract(worker, starts_on=date(2026, 4, 1), ends_on=None)
    contracts.terminate(first, on=date(2026, 9, 30), reason=TerminationReason.JOB_CHANGE, by=None)
    contracts.validate_no_overlap(worker.pk, date(2026, 10, 1), None)
    make_contract(
        worker,
        site=make_site(make_host(name="転職先", corporate_number=VALID_NUMBERS[1])),
        starts_on=date(2026, 10, 1),
        ends_on=None,
    )
    with pytest.raises(ValidationError):
        contracts.cancel_termination(
            first, by=None, reason="入力の誤り"
        )  # 取り消すと次の契約と重なる


def test_extending_by_new_version_cannot_overlap_next_contract() -> None:
    worker = make_worker()
    first = make_contract(worker, starts_on=date(2025, 4, 1), ends_on=date(2026, 3, 31))
    site = first.versions.get().site
    make_contract(worker, site=site, starts_on=date(2026, 4, 1), ends_on=None)
    with pytest.raises(ValidationError):
        contracts.validate_no_overlap(
            worker.pk, date(2025, 4, 1), date(2026, 9, 30), exclude_contract_id=first.pk
        )


# --- 派遣の不可・業務区分・変更理由・終了 ----------------------------------------------


def test_site_must_belong_to_contracting_host() -> None:
    site = site_with_categories()
    other_site = make_site(make_host(name="別会社", corporate_number=VALID_NUMBERS[1]))
    contracts.validate_site_belongs_to_host(site.host_organization, site)
    with pytest.raises(ValidationError, match="派遣"):
        contracts.validate_site_belongs_to_host(site.host_organization, other_site)


def test_categories_must_be_registered_for_site() -> None:
    site = site_with_categories()
    contracts.validate_categories_of_site(site, [category("sewing")])
    with pytest.raises(ValidationError, match="家具製造"):
        contracts.validate_categories_of_site(site, [category("furniture")])


def test_change_reason_required_from_second_version() -> None:
    contracts.validate_change(None, "")
    contract = make_contract()
    with pytest.raises(ValidationError):
        contracts.validate_change(contract, " ")


@pytest.mark.parametrize(
    ("reason", "note", "ok"),
    [
        ("", "", False),
        ("voluntary", "", True),
        ("other", "", False),
        ("other", "在留資格の喪失", True),
    ],
)
def test_termination_reason_rules(reason: str, note: str, ok: bool) -> None:
    def check() -> None:
        contracts.validate_termination(date(2026, 9, 30), reason, note, date(2026, 4, 1))

    if ok:
        check()
    else:
        with pytest.raises(ValidationError):
            check()


def test_termination_before_start_is_rejected() -> None:
    with pytest.raises(ValidationError):
        contracts.validate_termination(date(2026, 3, 1), "voluntary", "", date(2026, 4, 1))


# --- 同等報酬の根拠 --------------------------------------------------------------


def test_equal_pay_evidence() -> None:
    contract = make_contract(comparable_japanese_note="同職位の日本人（入社3年目）")
    version = contract.versions.get()
    assert contracts.equal_pay_evidence_missing(version)  # 資料が無い
    create_attachment(
        target=version,
        kind=AttachmentKind.EQUAL_PAY_EVIDENCE,
        file=ContentFile(PDF_BYTES, name="wage.pdf"),
        created_by=None,
    )
    assert not contracts.equal_pay_evidence_missing(version)
    assert contracts.equal_pay_evidence_missing(
        make_contract(
            make_worker(name_romaji="X"),
            site=make_site(make_host(name="C", corporate_number=VALID_NUMBERS[1])),
        ).versions.get()
    )


def test_array_fields_are_stored() -> None:
    version = make_contract(
        renewal_criteria=["workload", "performance"],
        regular_holiday_weekdays=["sat", "sun"],
        insurances=["employees_pension", "health", "employment", "workers_compensation"],
    ).versions.get()
    stored = EmploymentContractVersion.objects.get(pk=version.pk)
    assert stored.regular_holiday_weekdays == ["sat", "sun"]
    assert len(stored.insurances) == 4
