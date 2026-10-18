"""雇用契約の登録・編集の画面（T28b）。

契約の登録・新しい版（写した値・差分なしの拒否・別の雇用主）・終了・終了の取消（管理者・理由）・
根拠資料・権限・無効化した外国人・監査ログ。
"""

from datetime import date, time
from decimal import Decimal
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from apps.contracts.models import EmploymentContract
from apps.core.models import AttachmentKind, AuditAction, AuditLog
from apps.filings.models import Filing, FilingKind, FilingStatus
from apps.workers.models import ForeignWorker
from services import contracts, workers
from tests.conftest import PDF_BYTES
from tests.contracts.factories import make_contract, site_with_categories, version_values
from tests.hosts.factories import category, make_host, make_site
from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db


def to_post(values: dict[str, Any]) -> dict[str, Any]:
    """モデルの値を画面のフォームの送信の値にする（False のチェックボックスは送らない）。"""
    data: dict[str, Any] = {}
    for name, value in values.items():
        if value is None or value is False:
            continue
        if value is True:
            data[name] = "on"
        elif isinstance(value, (date, time)):
            data[name] = value.isoformat()[:5] if isinstance(value, time) else value.isoformat()
        elif isinstance(value, list):
            data[name] = [getattr(item, "pk", item) for item in value]
        elif isinstance(value, Decimal):
            data[name] = str(value)
        else:
            data[name] = getattr(value, "pk", value)
    return data


def rows(prefix: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    data = {
        f"{prefix}-TOTAL_FORMS": str(len(items)),
        f"{prefix}-INITIAL_FORMS": str(len(items)),
        f"{prefix}-MIN_NUM_FORMS": "0",
        f"{prefix}-MAX_NUM_FORMS": "1000",
    }
    for i, item in enumerate(items):
        data |= {f"{prefix}-{i}-{key}": value for key, value in to_post(item).items()}
    return data


def submit(client: Client, url: str, data: dict[str, Any], **kwargs: Any) -> Any:
    page = client.get(url)
    assert page.status_code == 200, page.status_code
    return client.post(url, {"once_token": page.context["once_token"], **data}, **kwargs)


def first_version_data(site: Any) -> dict[str, Any]:
    values = version_values(site, renewal_criteria=["workload"]) | {
        "categories": [category("machine_metal_processing")]
    }
    return (
        to_post(values)
        | rows("shifts", [])
        | rows("allowances", [{"name": "住宅手当", "amount": 10000}])
        | rows("deductions", [{"kind": "housing", "amount": 30000}])
    )


@pytest.fixture
def worker() -> ForeignWorker:
    return make_worker()


def test_register_a_contract_from_the_contracts_tab(
    support_staff_client: Client, worker: ForeignWorker
) -> None:
    site = site_with_categories()
    tab = support_staff_client.get(f"/workers/{worker.pk}/contracts/").content.decode()
    assert f'href="/workers/{worker.pk}/contracts/new/"' in tab
    response = submit(
        support_staff_client,
        f"/workers/{worker.pk}/contracts/new/",
        first_version_data(site),
        follow=True,
    )
    contract = EmploymentContract.objects.get()
    assert contract.host_organization == site.host_organization  # 事業所から決まる
    version = contract.versions.get()
    assert version.allowances.get().name == "住宅手当"
    assert version.changed_on == version.starts_on  # 空なら開始日
    assert any("第 1 版" in str(m) for m in response.context["messages"])
    assert AuditLog.objects.filter(
        action=AuditAction.CREATE, content_type__model="employmentcontractversion"
    ).exists()


def test_new_version_copies_the_previous_one_and_raises_the_change_filing(
    support_staff_client: Client, worker: ForeignWorker
) -> None:
    contract = make_contract(worker, renewal_criteria=["workload"])
    previous = contract.versions.get()
    url = f"/workers/{worker.pk}/contracts/{contract.pk}/versions/new/"
    page = support_staff_client.get(url)
    form = page.context["editor"].form
    assert form.initial["base_wage"] == previous.base_wage
    assert "changed_on" not in form.initial and "change_reason" not in form.initial
    allowances = page.context["editor"].allowances
    assert [row.initial["name"] for row in allowances.forms] == ["住宅手当"]

    values = contracts.initial_from(previous) | {
        "base_wage": 200000,  # 減額は変更届出が必要
        "changed_on": date(2026, 10, 1),
        "change_reason": "基本給の改定",
    }
    details = contracts.detail_initial(previous)
    data = (
        to_post(values)
        | rows("shifts", details["shifts"])
        | rows("allowances", details["allowances"])
        | rows("deductions", details["deductions"])
    )
    response = submit(support_staff_client, url, data, follow=True)
    assert contract.versions.count() == 2
    second = contracts.latest_version(contract)
    assert second is not None
    assert [a.name for a in second.allowances.all()] == ["住宅手当"]  # 写した行も保存する
    assert second.deductions.count() == 1
    filing = Filing.objects.get(kind=FilingKind.CONTRACT_CHANGE)
    assert filing.basis_on == date(2026, 10, 1)
    assert any("雇用契約変更届出" in str(m) for m in response.context["messages"])

    # 変更の無い版は保存できない
    data |= {"changed_on": "2026-11-01", "change_reason": "確認"}
    response = submit(support_staff_client, url, data)
    assert response.status_code == 200
    assert "前の版と変わった所がありません" in response.content.decode()
    assert contract.versions.count() == 2


def test_new_version_offers_only_sites_of_the_same_employer(
    support_staff_client: Client, worker: ForeignWorker
) -> None:
    contract = make_contract(worker, renewal_criteria=["workload"])
    other = make_site(make_host(name="別の会社", corporate_number="7999900000010"))
    url = f"/workers/{worker.pk}/contracts/{contract.pk}/versions/new/"
    page = support_staff_client.get(url)
    choices = [value for value, _label in page.context["editor"].form.fields["site"].choices]
    assert other.pk not in [getattr(v, "value", v) for v in choices]


def test_terminate_and_cancel_the_termination(
    support_staff_client: Client, admin_client: Client, worker: ForeignWorker
) -> None:
    contract = make_contract(worker, renewal_criteria=["workload"])
    url = f"/workers/{worker.pk}/contracts/{contract.pk}/terminate/"
    response = submit(
        support_staff_client,
        url,
        {"terminated_on": "2026-09-30", "termination_reason": "company"},
    )
    assert response.status_code == 302
    kinds = set(Filing.objects.values_list("kind", flat=True))
    assert {FilingKind.CONTRACT_TERMINATION, FilingKind.ACCEPTANCE_DIFFICULTY} <= kinds

    reopen = f"/workers/{worker.pk}/contracts/{contract.pk}/reopen/"
    assert support_staff_client.get(reopen).status_code == 403  # 管理者だけ
    response = submit(admin_client, reopen, {"reason": ""})
    assert "reason" in response.context["form"].errors
    response = submit(admin_client, reopen, {"reason": "別の契約に登録した"})
    assert response.status_code == 302
    contract.refresh_from_db()
    assert contract.terminated_on is None
    task = Filing.objects.get(kind=FilingKind.CONTRACT_TERMINATION)
    assert task.status == FilingStatus.CANCELLED and "別の契約に登録した" in task.status_reason


def test_attach_and_void_documents(support_staff_client: Client, worker: ForeignWorker) -> None:
    contract = make_contract(worker, renewal_criteria=["workload"])
    url = f"/workers/{worker.pk}/contracts/{contract.pk}/versions/1/attachments/new/"
    response = submit(
        support_staff_client,
        url,
        {
            "kind": AttachmentKind.EQUAL_PAY_EVIDENCE,
            "uploads": [SimpleUploadedFile("賃金規程.pdf", PDF_BYTES, "application/pdf")],
        },
    )
    assert response.status_code == 302
    attachment = contract.versions.get().attachments.get()
    void = f"/workers/{worker.pk}/contracts/{contract.pk}/attachments/{attachment.pk}/void/"
    response = submit(support_staff_client, void, {"reason": "別の契約の資料"})
    assert response.status_code == 302
    attachment.refresh_from_db()
    assert attachment.voided_at is not None

    # 新しい版の画面では、前の版の資料を参照として出す（写さない）
    page = support_staff_client.get(f"/workers/{worker.pk}/contracts/{contract.pk}/versions/new/")
    assert page.context["previous_attachments"] == [attachment]
    assert 'id="previous-attachments"' in page.content.decode()


def test_permissions_and_deactivated_workers(
    support_staff_client: Client, worker: ForeignWorker
) -> None:
    contract = make_contract(worker, renewal_criteria=["workload"])
    workers.deactivate(worker, by=None, reason="重複")
    for url in (
        f"/workers/{worker.pk}/contracts/new/",
        f"/workers/{worker.pk}/contracts/{contract.pk}/versions/new/",
        f"/workers/{worker.pk}/contracts/{contract.pk}/terminate/",
    ):
        assert support_staff_client.get(url).status_code == 403
    tab = support_staff_client.get(f"/workers/{worker.pk}/contracts/").content.decode()
    assert "/contracts/new/" not in tab and "/admin/" not in tab.split("<main")[1]


def test_conversion_fields_show_on_the_tab(
    support_staff_client: Client, worker: ForeignWorker
) -> None:
    site = site_with_categories()
    data = first_version_data(site) | {
        "indefinite_conversion": "on",
        "conversion_conditions_change": "changed",
    }
    response = submit(
        support_staff_client, f"/workers/{worker.pk}/contracts/new/", data, follow=True
    )
    messages = [str(m) for m in response.context["messages"]]
    assert any("別紙2" in m for m in messages)
    html = response.content.decode()
    assert "2027-04-01" in html  # 転換の日＝終了日（2027-03-31）の翌日
    assert 'id="contract-warnings"' in html
    # 期間の定めの無い契約では無期転換の欄を選べない
    response = submit(
        support_staff_client,
        f"/workers/{make_worker(name_romaji='B', name_kanji='').pk}/contracts/new/",
        first_version_data(site) | {"ends_on": "", "indefinite_conversion": "on"},
    )
    assert "indefinite_conversion" in response.context["editor"].form.errors
    page = support_staff_client.get(f"/workers/{worker.pk}/contracts/new/").content.decode()
    assert 'data-enabled-when="ends_on=*"' in page
