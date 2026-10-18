"""雇用契約の版のサービス（T28b）: 検査の共通化（validate_version）・条件つきの必須・同じ雇用主・
差分の無い版の拒否・前の版を写す初期値・無期転換の通算期間の警告・終了の取消の理由・根拠資料。"""

from datetime import date

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile

from apps.contracts.models import (
    EmploymentContract,
    EmploymentContractAllowance,
    EmploymentContractDeduction,
    EmploymentContractVersion,
)
from apps.core.models import AttachmentKind, AuditAction, AuditLog
from apps.filings.models import Filing, FilingKind, FilingStatus
from apps.hosts.models import HostOrganization, OperationCategory
from apps.workers.models import ForeignWorker
from services import contracts
from tests.conftest import PDF_BYTES
from tests.contracts.factories import make_contract, site_with_categories, version_values
from tests.hosts.factories import category, make_host, make_site
from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db


def next_version(contract: EmploymentContract, **overrides: object) -> EmploymentContractVersion:
    latest = contracts.latest_version(contract)
    assert latest is not None
    values = version_values(latest.site, change_reason="変更") | overrides
    return EmploymentContractVersion(**values)


def save(
    version: EmploymentContractVersion,
    contract: EmploymentContract | None,
    *,
    details: contracts.Details | None = None,
    categories: list[OperationCategory] | None = None,
    worker: ForeignWorker | None = None,
    host_organization: HostOrganization | None = None,
) -> contracts.VersionSaved:
    return contracts.create_version(
        version,
        contract=contract,
        worker=worker,
        host_organization=host_organization,
        categories=categories or [category("machine_metal_processing")],
        details=details
        or contracts.Details(
            allowances=[EmploymentContractAllowance(name="住宅手当", amount=10000)]
        ),
        by=None,
    )


# --- 条件つきの必須 ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"renewal": "possible", "renewal_criteria": []}, "renewal_criteria"),
        ({"renewal": "none", "renewal_criteria": ["workload"]}, "renewal_criteria"),
        ({"renewal_criteria": ["other"]}, "renewal_criteria_other"),
        ({"has_renewal_limit": True}, "renewal_limit_count"),
        ({"has_early_paid_leave": True, "early_paid_leave_days": 3}, "early_paid_leave_days"),
        ({"has_raise": True}, "raise_note"),
        ({"has_bonus": True}, "bonus_note"),
        ({"has_retirement_allowance": True}, "retirement_allowance_note"),
        ({"indefinite_conversion": True, "ends_on": None}, "indefinite_conversion"),
        ({"indefinite_conversion": True}, "conversion_conditions_change"),
        ({"conversion_conditions_change": "none"}, "conversion_conditions_change"),
    ],
)
def test_conditional_items(overrides: dict[str, object], field: str) -> None:
    site = site_with_categories()
    values = version_values(site, renewal_criteria=["workload"]) | overrides
    errors = contracts.conditional_errors(EmploymentContractVersion(**values))
    assert field in errors


def test_complete_version_has_no_conditional_errors() -> None:
    site = site_with_categories()
    values = version_values(
        site,
        renewal_criteria=["workload"],
        indefinite_conversion=True,
        conversion_conditions_change="none",
        has_raise=True,
        raise_note="年1回",
    )
    assert contracts.conditional_errors(EmploymentContractVersion(**values)) == {}


# --- 版の保存 --------------------------------------------------------------------


def test_new_contract_and_a_changed_version_raise_the_filing() -> None:
    site = site_with_categories()
    worker = make_worker()
    first = EmploymentContractVersion(**version_values(site, renewal_criteria=["workload"]))
    saved = save(first, None, worker=worker, host_organization=site.host_organization)
    contract = saved.version.contract
    assert saved.version.version == 1 and contract.host_organization == site.host_organization
    assert contract.versions.get().allowances.count() == 1

    second = next_version(
        contract, renewal_criteria=["workload"], base_wage=200000, changed_on=date(2026, 10, 1)
    )
    saved = save(second, contract)
    assert saved.version.version == 2
    assert saved.filing is not None and saved.filing.kind == FilingKind.CONTRACT_CHANGE


def test_version_without_any_change_is_not_saved() -> None:
    contract = make_contract(renewal_criteria=["workload"])  # 第1版: 住宅手当・控除あり
    first = contract.versions.get()
    values = contracts.initial_from(first)
    values.pop("categories")
    copy = EmploymentContractVersion(
        **(values | {"site": first.site, "changed_on": date(2026, 10, 1)}),
        change_reason="確認",
    )
    rows = contracts.detail_initial(first)
    details = contracts.Details(
        allowances=[EmploymentContractAllowance(**row) for row in rows["allowances"]],
        deductions=[EmploymentContractDeduction(**row) for row in rows["deductions"]],
    )
    audit_count = AuditLog.objects.count()
    with pytest.raises(ValidationError) as error:
        save(copy, contract, details=details, categories=list(first.categories.all()))
    assert error.value.messages == [
        "前の版と変わった所がありません。少なくとも 1 つの項目を変えてください。"
    ]
    assert contract.versions.count() == 1  # 巻き戻した
    assert AuditLog.objects.count() == audit_count


def test_new_version_cannot_move_to_another_employer() -> None:
    contract = make_contract()
    other_site = make_site(make_host(name="別の会社", corporate_number="7999900000010"))
    other_site.categories.set([category("machine_metal_processing")])
    version = next_version(contract, renewal_criteria=["workload"], site=other_site)
    with pytest.raises(ValidationError) as error:
        save(version, contract)
    assert "site" in error.value.error_dict
    assert "新しい契約" in str(error.value.error_dict["site"]) or error.value.error_dict["site"]
    assert contract.versions.count() == 1


def test_initial_values_copy_the_previous_version_except_the_change() -> None:
    contract = make_contract()
    first = contract.versions.get()
    values = contracts.initial_from(first)
    assert values["base_wage"] == first.base_wage
    assert values["site"] == first.site_id
    for name in ("changed_on", "change_reason", "signed_on", "version", "contract"):
        assert name not in values
    rows = contracts.detail_initial(first)
    assert rows["allowances"] == [
        {
            "name": "住宅手当",
            "amount": 10000,
            "calculation_method": "",
            "fixed_overtime_hours": None,
        }
    ]


# --- 無期転換の通算期間の警告（目安）-----------------------------------------------------


def test_conversion_warning_counts_registered_contracts_with_the_same_host() -> None:
    worker = make_worker()
    site = site_with_categories()
    # 同じ受入れ機関で 3 年＋ 3 年（終了済み → 次の契約）
    old = make_contract(
        worker,
        site=site,
        starts_on=date(2020, 4, 1),
        ends_on=date(2023, 3, 31),
        changed_on=date(2020, 4, 1),
    )
    contracts.terminate(old, on=date(2023, 3, 31), reason="expiry", by=None)
    current = make_contract(
        worker,
        site=site,
        starts_on=date(2023, 4, 1),
        ends_on=date(2026, 3, 31),
        changed_on=date(2023, 4, 1),
    )
    version = current.versions.get()
    warning = contracts.conversion_warning(version)
    assert "5 年" in warning or "5 years" in warning
    assert "クーリング" in warning or "cooling" in warning

    # 別の受入れ機関の契約は数えない
    other_worker = make_worker(name_romaji="LI LEI", name_kanji="李雷")
    other_site = make_site(make_host(name="別の会社", corporate_number="7999900000010"))
    other_site.categories.set([category("machine_metal_processing")])
    previous = make_contract(
        other_worker,
        site=other_site,
        starts_on=date(2020, 4, 1),
        ends_on=date(2023, 3, 31),
        changed_on=date(2020, 4, 1),
    )
    contracts.terminate(previous, on=date(2023, 3, 31), reason="expiry", by=None)
    new = make_contract(
        other_worker,
        site=site,
        starts_on=date(2023, 4, 1),
        ends_on=date(2026, 3, 31),
        changed_on=date(2023, 4, 1),
    )
    assert contracts.conversion_warning(new.versions.get()) == ""


def test_no_warning_when_marked_or_without_an_end_date() -> None:
    contract = make_contract(
        starts_on=date(2020, 4, 1), ends_on=date(2026, 3, 31), changed_on=date(2020, 4, 1)
    )
    version = contract.versions.get()
    assert contracts.conversion_warning(version)
    version.indefinite_conversion = True
    assert contracts.conversion_warning(version) == ""
    open_ended = make_contract(
        make_worker(name_romaji="NO END", name_kanji=""),
        site=contract.versions.get().site,
        starts_on=date(2019, 4, 1),
        ends_on=None,
        changed_on=date(2019, 4, 1),
    )
    assert contracts.conversion_warning(open_ended.versions.get()) == ""


# --- 終了の取消・根拠資料 ----------------------------------------------------------


def test_cancelling_a_termination_needs_a_reason_and_keeps_it() -> None:
    contract = make_contract()
    contracts.terminate(contract, on=date(2026, 9, 30), reason="company", by=None)
    with pytest.raises(ValidationError):
        contracts.cancel_termination(contract, by=None, reason=" ")
    contracts.cancel_termination(contract, by=None, reason="別の契約に登録した")
    task = Filing.objects.get(kind=FilingKind.CONTRACT_TERMINATION)
    assert task.status == FilingStatus.CANCELLED
    assert "別の契約に登録した" in task.status_reason
    log = AuditLog.objects.filter(
        action=AuditAction.UPDATE,
        content_type=ContentType.objects.get_for_model(EmploymentContract),
    ).latest("pk")
    assert log.context["cancel_termination"] == "別の契約に登録した"
    assert log.changes["terminated_on"] == ["2026-09-30", None]


def test_version_attachments_use_the_allowed_kinds() -> None:
    version = make_contract().versions.get()
    file = ContentFile(PDF_BYTES, name="別紙2.pdf")
    (attachment,) = contracts.add_version_attachments(
        version, AttachmentKind.INDEFINITE_CONVERSION_TERMS, [file], by=None
    )
    assert attachment.kind == AttachmentKind.INDEFINITE_CONVERSION_TERMS
    with pytest.raises(ValidationError):
        contracts.add_version_attachments(
            version, AttachmentKind.HEALTH_CHECK_RECORD, [file], by=None
        )
    version.conversion_conditions_change = "changed"
    assert not contracts.conversion_terms_missing(version)
    attachment.voided_at = attachment.created_at
    attachment.save()
    assert contracts.conversion_terms_missing(version)
