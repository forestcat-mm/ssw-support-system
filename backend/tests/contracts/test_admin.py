"""雇用契約の admin（契約の作成・報酬変更で版が増え差分が取れる・終了・権限・監査ログ）。"""

import re
from datetime import date
from typing import Any

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from apps.contracts.models import EmploymentContract, EmploymentContractVersion
from apps.core.models import AttachmentKind, AuditAction, AuditLog, User
from services import contracts
from tests.conftest import PDF_BYTES
from tests.core.factories import make_user, verified_client
from tests.hosts.factories import VALID_NUMBERS, category, make_host, make_site
from tests.workers.factories import make_worker

from .factories import make_contract, site_with_categories

pytestmark = pytest.mark.django_db

VERSION_URL = "/admin/contracts/employmentcontractversion/"
CONTRACT_URL = "/admin/contracts/employmentcontract/"
ATTACHMENTS = "core-attachment-content_type-object_id"


def _errors(response: Any) -> list[str]:
    html = response.content.decode()
    return [
        re.sub(r"<[^>]+>", " ", block).strip()
        for block in re.findall(r'<ul class="errorlist[^"]*"[^>]*>(.*?)</ul>', html, re.S)
    ]


def _inline(prefix: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    data: dict[str, Any] = {
        f"{prefix}-TOTAL_FORMS": str(len(rows)),
        f"{prefix}-INITIAL_FORMS": "0",
        f"{prefix}-MIN_NUM_FORMS": "0",
        f"{prefix}-MAX_NUM_FORMS": "1000",
    }
    for i, row in enumerate(rows):
        data |= {f"{prefix}-{i}-{k}": v for k, v in row.items()}
    return data


def _version_form(site: Any, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "changed_on": "2026-04-01",
        "starts_on": "2026-04-01",
        "ends_on": "2027-03-31",
        "renewal": "possible",
        "renewal_criteria": ["workload", "performance"],
        "site": site.pk,
        "categories": [category("machine_metal_processing").pk],
        "duties": "機械金属加工の作業",
        "start_time": "08:30",
        "end_time": "17:30",
        "daily_working_minutes": "480",
        "break_minutes": "60",
        "weekly_working_minutes": "2400",
        "weekly_working_days": "5",
        "has_overtime": "on",
        "regular_holiday_weekdays": ["sat", "sun"],
        "national_holidays_off": "on",
        "annual_holidays": "120",
        "paid_leave_days": "10",
        "wage_type": "monthly",
        "base_wage": "220000",
        "overtime_rate_within_60h": "25",
        "wage_closing_day": "毎月末日",
        "wage_payment_day": "翌月25日",
        "payment_method": "bank_transfer",
        "insurances": ["employees_pension", "health", "employment", "workers_compensation"],
        "comparable_japanese_note": "同職位の日本人（入社3年目）",
        "comparable_japanese_wage": "215000",
    }
    data |= _inline("shifts", [])
    data |= _inline(
        "allowances", [{"name": "住宅手当", "amount": "10000", "calculation_method": "定額"}]
    )
    data |= _inline("deductions", [{"kind": "housing", "amount": "30000"}])
    data |= _inline(ATTACHMENTS, [])
    return data | overrides


def test_create_contract_with_first_version(admin_client: Client, admin_user: User) -> None:
    worker, site = make_worker(), site_with_categories()
    data = _version_form(site) | {
        "new_worker": worker.pk,
        "new_host_organization": site.host_organization.pk,
    }
    response = admin_client.post(f"{VERSION_URL}add/", data)
    assert response.status_code == 302, _errors(response)
    contract = EmploymentContract.objects.get()
    version = contract.versions.get()
    assert (version.version, version.changed_on) == (1, date(2026, 4, 1))
    assert contract.current_ends_on == date(2027, 3, 31)
    assert version.created_by == admin_user
    assert list(version.allowances.values_list("name", "amount")) == [("住宅手当", 10000)]
    assert version.regular_holiday_weekdays == ["sat", "sun"]
    assert contracts.monthly_total(version) == 230000


def test_contract_add_redirects_to_version_add(admin_client: Client) -> None:
    response = admin_client.get(f"{CONTRACT_URL}add/")
    assert response.status_code == 302
    assert response["Location"] == f"{VERSION_URL}add/"


def test_wage_change_adds_version_with_diff(admin_client: Client) -> None:
    """T09 の完了条件: 報酬変更で版が増え、差分が取れる。"""
    contract = make_contract()
    v1 = contract.versions.get()

    prefilled = admin_client.get(f"{VERSION_URL}add/", {"contract": contract.pk})
    assert prefilled.status_code == 200
    form = prefilled.context["adminform"].form
    assert form.initial["base_wage"] == 220000
    assert form.initial["contract_choice"] == contract.pk
    assert 'value="住宅手当"' in prefilled.content.decode()  # 手当の明細も引き継ぐ

    data = _version_form(v1.site) | {
        "contract_choice": contract.pk,
        "change_reason": "昇給",
        "changed_on": "2027-04-01",
        "base_wage": "230000",
    }
    data |= _inline(
        "allowances", [{"name": "住宅手当", "amount": "12000", "calculation_method": ""}]
    )
    response = admin_client.post(f"{VERSION_URL}add/", data)
    assert response.status_code == 302, _errors(response)

    v2 = contract.versions.get(version=2)
    assert EmploymentContractVersion.objects.get(pk=v1.pk).base_wage == 220000  # 旧版はそのまま
    changes = contracts.diff(v1, v2)
    assert changes["base_wage"] == [220000, 230000]
    assert changes["allowances"][1] == [("住宅手当", 12000, "", None)]
    page = admin_client.get(f"{VERSION_URL}{v2.pk}/change/").content.decode()
    assert "前の版からの変更点" in page
    assert "<td>220000</td><td>230000</td>" in page


def test_second_version_requires_change_reason(admin_client: Client) -> None:
    contract = make_contract()
    data = _version_form(contract.versions.get().site) | {"contract_choice": contract.pk}
    response = admin_client.post(f"{VERSION_URL}add/", data)
    assert response.status_code == 200
    assert any("変更理由を入力してください" in e for e in _errors(response))
    assert contract.versions.count() == 1


def test_dispatch_and_categories_are_rejected(admin_client: Client) -> None:
    worker, site = make_worker(), site_with_categories()
    other_site = make_site(make_host(name="派遣先", corporate_number=VALID_NUMBERS[1]))
    data = _version_form(other_site) | {
        "new_worker": worker.pk,
        "new_host_organization": site.host_organization.pk,
    }
    response = admin_client.post(f"{VERSION_URL}add/", data)
    assert any("派遣は不可" in e for e in _errors(response))

    data = _version_form(site, categories=[category("furniture").pk]) | {
        "new_worker": worker.pk,
        "new_host_organization": site.host_organization.pk,
    }
    response = admin_client.post(f"{VERSION_URL}add/", data)
    assert any("事業所に登録されていない業務区分です" in e for e in _errors(response))
    assert not EmploymentContract.objects.exists()


def test_overlapping_contract_is_rejected_with_message(admin_client: Client) -> None:
    worker = make_worker()
    first = make_contract(worker)
    site = make_site(make_host(name="別会社", corporate_number=VALID_NUMBERS[1]))
    site.categories.set([category("machine_metal_processing")])
    data = _version_form(site, starts_on="2026-10-01", ends_on="") | {
        "new_worker": worker.pk,
        "new_host_organization": site.host_organization.pk,
    }
    response = admin_client.post(f"{VERSION_URL}add/", data)
    assert response.status_code == 200
    assert any(f"雇用契約（ID: {first.pk}）と期間が重なっています" in e for e in _errors(response))


def _termination(contract: EmploymentContract, **values: str) -> dict[str, Any]:
    data: dict[str, Any] = {
        "terminated_on": "",
        "termination_reason": "",
        "termination_note": "",
    } | values
    return (
        data
        | _inline("versions", [])
        | {
            "versions-TOTAL_FORMS": str(contract.versions.count()),
            "versions-INITIAL_FORMS": str(contract.versions.count()),
            **{f"versions-{i}-id": v.pk for i, v in enumerate(contract.versions.order_by("pk"))},
        }
    )


def test_terminate_contract_and_cancel_only_by_admin(
    admin_client: Client, admin_user: User
) -> None:
    contract = make_contract()
    url = f"{CONTRACT_URL}{contract.pk}/change/"
    response = admin_client.post(url, _termination(contract, terminated_on="2026-09-30"))
    assert any("終了理由を選んでください" in e for e in _errors(response))
    response = admin_client.post(
        url, _termination(contract, terminated_on="2026-09-30", termination_reason="other")
    )
    assert any("終了理由のメモ" in e for e in _errors(response))

    response = admin_client.post(
        url, _termination(contract, terminated_on="2026-09-30", termination_reason="voluntary")
    )
    assert response.status_code == 302, _errors(response)
    contract.refresh_from_db()
    assert contracts.status_of(contract, date(2026, 10, 16)) is contracts.ContractStatus.TERMINATED
    log = AuditLog.objects.filter(
        action=AuditAction.UPDATE,
        content_type=ContentType.objects.get_for_model(EmploymentContract),
    ).latest("pk")
    assert log.changes["terminated_on"] == [None, "2026-09-30"]
    assert log.changes["termination_reason"] == ["", "voluntary"]
    assert log.actor == admin_user

    staff = verified_client(make_user("tanto", roles=("support_staff",)))
    response = staff.post(url, _termination(contract))
    assert any("終了の取り消しは管理者だけ" in e for e in _errors(response))
    assert admin_client.post(url, _termination(contract)).status_code == 302
    contract.refresh_from_db()
    assert contract.terminated_on is None


def test_existing_version_is_read_only_but_evidence_can_be_attached(admin_client: Client) -> None:
    version = make_contract().versions.get()
    url = f"{VERSION_URL}{version.pk}/change/"
    page = admin_client.get(url)
    assert page.status_code == 200
    assert "base_wage" not in page.context["adminform"].form.fields

    data = _inline(
        ATTACHMENTS,
        [
            {
                "kind": AttachmentKind.EQUAL_PAY_EVIDENCE,
                "file": SimpleUploadedFile("wage.pdf", PDF_BYTES, content_type="application/pdf"),
            }
        ],
    )
    data |= {"base_wage": "1"}  # 読み取り専用なので無視される
    for prefix in ("shifts", "allowances", "deductions"):
        rows = getattr(version, prefix).count()
        data |= {
            f"{prefix}-TOTAL_FORMS": str(rows),
            f"{prefix}-INITIAL_FORMS": str(rows),
            f"{prefix}-MIN_NUM_FORMS": "0",
            f"{prefix}-MAX_NUM_FORMS": "1000",
        }
        data |= {
            f"{prefix}-{i}-id": row.pk
            for i, row in enumerate(getattr(version, prefix).order_by("pk"))
        }
    response = admin_client.post(url, data)
    assert response.status_code == 302, _errors(response)
    assert EmploymentContractVersion.objects.get(pk=version.pk).base_wage == 220000
    assert version.attachments.filter(kind=AttachmentKind.EQUAL_PAY_EVIDENCE).count() == 1
    assert admin_client.get(f"{VERSION_URL}{version.pk}/delete/").status_code == 403
    assert admin_client.get(f"{CONTRACT_URL}{version.contract_id}/delete/").status_code == 403


def test_viewing_contract_and_version_is_audited(admin_client: Client) -> None:
    contract = make_contract()
    admin_client.get(f"{CONTRACT_URL}{contract.pk}/change/")
    admin_client.get(f"{VERSION_URL}{contract.versions.get().pk}/change/")
    viewed = set(
        AuditLog.objects.filter(action=AuditAction.VIEW).values_list(
            "content_type__model", flat=True
        )
    )
    assert viewed == {"employmentcontract", "employmentcontractversion"}


def test_list_shows_status_and_filters(admin_client: Client) -> None:
    active = make_contract(starts_on=date(2025, 4, 1), ends_on=None)
    scheduled = make_contract(
        make_worker(name_romaji="LI NA"),
        site=make_site(make_host(name="B", corporate_number=VALID_NUMBERS[1])),
        starts_on=date(2099, 4, 1),
        ends_on=None,
    )
    page = admin_client.get(CONTRACT_URL, {"status": "scheduled"})
    assert [c.pk for c in page.context["cl"].result_list] == [scheduled.pk]
    assert "有効" in admin_client.get(CONTRACT_URL).content.decode()
    assert active.pk in [
        c.pk for c in admin_client.get(CONTRACT_URL, {"status": "active"}).context["cl"].result_list
    ]


def test_permissions(support_staff_client: Client) -> None:
    worker, site = make_worker(), site_with_categories()
    data = _version_form(site) | {
        "new_worker": worker.pk,
        "new_host_organization": site.host_organization.pk,
    }
    assert support_staff_client.post(f"{VERSION_URL}add/", data).status_code == 302
    assert verified_client(make_user("norole")).get(CONTRACT_URL, follow=True).status_code == 403
