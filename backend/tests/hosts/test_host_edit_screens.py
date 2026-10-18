"""受入れ機関の登録・編集の画面（T28a）: 登録・編集・添付・取消・権限・二重送信・監査ログ・
保存のときのお知らせ。"""

from datetime import date, timedelta
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone

from apps.core.models import AttachmentKind, AuditAction, AuditLog
from apps.filings.models import Filing, FilingKind, FilingStatus
from apps.hosts.models import (
    HostCooperationConfirmation,
    HostMembership,
    HostOrganization,
    HostSite,
    SupportContract,
)
from tests.conftest import PDF_BYTES
from tests.contracts.factories import make_contract
from tests.hosts.factories import VALID_NUMBERS, category, field, make_host, make_site

pytestmark = pytest.mark.django_db


def pdf(name: str = "file.pdf") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, PDF_BYTES, content_type="application/pdf")


def submit(client: Client, url: str, data: dict[str, Any]) -> Any:
    """画面を開いて二重送信防止の値を取り、送る。"""
    page = client.get(url)
    assert page.status_code == 200
    return client.post(url, {"once_token": page.context["once_token"], **data})


def messages_of(response: Any) -> list[str]:
    return [str(message) for message in response.context["messages"]]


HOST_DATA = {
    "entity_type": "corporation",
    "name": "テスト工業株式会社",
    "name_kana": "テストコウギョウ",
    "corporate_number": "7-0000-1205-0002",
    "representative_title": "代表取締役",
    "representative_name": "山田 太郎",
    "postal_code": "100-0001",
    "address": "東京都千代田区千代田1-1",
    "phone_number": "03-0000-0000",
}


def test_register_and_edit_a_host(support_staff_client: Client) -> None:
    response = submit(support_staff_client, "/hosts/new/", HOST_DATA)
    host = HostOrganization.objects.get()
    assert response.status_code == 302
    assert response["Location"] == f"/hosts/{host.pk}/#host-basic"
    assert host.corporate_number == VALID_NUMBERS[0]  # 正規化して保存
    assert host.created_by is not None
    assert AuditLog.objects.filter(
        action=AuditAction.CREATE, object_id=str(host.pk), content_type__model="hostorganization"
    ).exists()

    response = submit(
        support_staff_client,
        f"/hosts/{host.pk}/edit/",
        HOST_DATA | {"phone_number": "03-1111-1111"},
    )
    assert response.status_code == 302
    host.refresh_from_db()
    assert host.phone_number == "03-1111-1111"
    update = AuditLog.objects.filter(action=AuditAction.UPDATE, object_id=str(host.pk)).get()
    assert update.changes["phone_number"] == ["03-0000-0000", "03-1111-1111"]


def test_corporate_number_is_checked_and_duplicates_are_rejected(
    support_staff_client: Client,
) -> None:
    response = submit(
        support_staff_client, "/hosts/new/", HOST_DATA | {"corporate_number": "1000012050002"}
    )
    assert response.status_code == 200 and "corporate_number" in response.context["form"].errors
    submit(support_staff_client, "/hosts/new/", HOST_DATA)
    response = submit(support_staff_client, "/hosts/new/", HOST_DATA | {"name": "別名"})
    assert response.status_code == 200
    assert response.context["form"].errors  # 同じ法人番号は登録できない
    # 個人事業主は空でよい。法人は空にできない
    response = submit(
        support_staff_client,
        "/hosts/new/",
        HOST_DATA | {"entity_type": "sole_proprietor", "corporate_number": "", "name": "個人"},
    )
    assert response.status_code == 302
    response = submit(
        support_staff_client, "/hosts/new/", HOST_DATA | {"corporate_number": "", "name": "法人"}
    )
    assert response.status_code == 200


def test_add_and_edit_sites_with_categories(support_staff_client: Client) -> None:
    host = make_host()
    data = {
        "name": "本社工場",
        "postal_code": "100-0001",
        "address": "東京都千代田区千代田1-1",
        "industry_code": "2194",
        "field": field("industrial_products").pk,
        "categories": [category("machine_metal_processing").pk, category("sewing").pk],
    }
    response = submit(support_staff_client, f"/hosts/{host.pk}/sites/new/", data)
    assert response.status_code == 302
    site = HostSite.objects.get()
    assert set(site.categories.values_list("code", flat=True)) == {
        "machine_metal_processing",
        "sewing",
    }
    # ほかの分野の業務区分は選べない
    response = submit(
        support_staff_client,
        f"/hosts/{host.pk}/sites/{site.pk}/edit/",
        data | {"categories": [category("food_manufacturing_general").pk], "is_active": "on"},
    )
    assert "categories" in response.context["form"].errors


def test_site_in_use_shows_the_restrictions_on_the_form(support_staff_client: Client) -> None:
    host = make_host()
    site = make_site(host)
    site.categories.set([category("machine_metal_processing"), category("sewing")])
    make_contract(site=site)
    response = submit(
        support_staff_client,
        f"/hosts/{host.pk}/sites/{site.pk}/edit/",
        {
            "name": site.name,
            "postal_code": site.postal_code,
            "address": site.address,
            "industry_code": site.industry_code,
            "field": field("industrial_products").pk,
            "categories": [category("sewing").pk],
            # is_active を送らない = 無効にする
        },
    )
    errors = response.context["form"].errors
    assert set(errors) == {"categories", "is_active"}


def test_moving_a_site_with_confirmations_asks_for_resubmission(
    support_staff_client: Client,
) -> None:
    host = make_host()
    site = make_site(host)
    site.categories.set([category("sewing")])
    HostCooperationConfirmation.objects.create(
        host_organization=host, site=site, municipality="千代田区", submitted_on=date(2025, 4, 1)
    )
    url = f"/hosts/{host.pk}/sites/{site.pk}/edit/"
    data = {
        "name": site.name,
        "postal_code": "105-0011",
        "address": "東京都港区芝公園1-1",
        "industry_code": site.industry_code,
        "field": site.field_id,
        "categories": [category("sewing").pk],
        "is_active": "on",
    }
    page = support_staff_client.get(url)
    response = support_staff_client.post(
        url, {"once_token": page.context["once_token"], **data}, follow=True
    )
    assert any("出し直" in message for message in messages_of(response))


def test_membership_with_certificate_and_replacement(support_staff_client: Client) -> None:
    host = make_host()
    today = timezone.localdate()
    response = support_staff_client.get(f"/hosts/{host.pk}/memberships/new/")
    response = support_staff_client.post(
        f"/hosts/{host.pk}/memberships/new/",
        {
            "once_token": response.context["once_token"],
            "field": field("food_manufacturing").pk,
            "joined_on": "2025-04-01",
            "valid_until": (today + timedelta(days=20)).isoformat(),
            "certificate": pdf("証明書.pdf"),
        },
        follow=True,
    )
    membership = HostMembership.objects.get()
    assert membership.attachments.get().kind == AttachmentKind.MEMBERSHIP_CERTIFICATE
    assert any("起票" in message for message in messages_of(response))
    task = Filing.objects.get(kind=FilingKind.MEMBERSHIP_RENEWAL)
    # 登録済みの分野は選べない
    page = support_staff_client.get(f"/hosts/{host.pk}/memberships/new/")
    assert field("food_manufacturing") not in page.context["form"].fields["field"].queryset

    url = f"/hosts/{host.pk}/memberships/{membership.pk}/certificate/"
    page = support_staff_client.get(url)
    response = support_staff_client.post(
        url,
        {
            "once_token": page.context["once_token"],
            "file": pdf("新しい証明書.pdf"),
            "valid_until": (today + timedelta(days=400)).isoformat(),
        },
        follow=True,
    )
    task.refresh_from_db()
    assert task.status == FilingStatus.COMPLETED
    assert membership.attachments.count() == 2  # 古い証明書は残る
    html = response.content.decode()
    assert "証明書.pdf" in html and "新しい証明書.pdf" in html


def test_cooperation_submission_with_copy_and_status(support_staff_client: Client) -> None:
    host = make_host()
    site = make_site(host)
    second = make_site(host, name="第二工場")
    page = support_staff_client.get(f"/hosts/{host.pk}/")
    assert page.content.decode().count('data-status="not-submitted"') == 2

    url = f"/hosts/{host.pk}/cooperation/new/"
    form = support_staff_client.get(url)
    response = support_staff_client.post(
        url,
        {
            "once_token": form.context["once_token"],
            "site": site.pk,
            "municipality": "港区",  # 住居地の市区町村でもよい
            "submitted_on": "2025-04-10",
            "uploads": [pdf("控え.pdf")],
        },
    )
    assert response.status_code == 302
    confirmation = HostCooperationConfirmation.objects.get()
    assert confirmation.attachments.get().kind == AttachmentKind.COOPERATION_CONFIRMATION
    html = support_staff_client.get(f"/hosts/{host.pk}/").content.decode()
    assert html.count('data-status="not-submitted"') == 1
    assert f'data-site="{second.pk}"' in html and "港区" in html

    future = (timezone.localdate() + timedelta(days=1)).isoformat()
    response = submit(
        support_staff_client,
        url,
        {"site": site.pk, "municipality": "港区", "submitted_on": future},
    )
    assert "submitted_on" in response.context["form"].errors


def test_support_contract_with_file_and_overlap(support_staff_client: Client) -> None:
    host = make_host()
    url = f"/hosts/{host.pk}/support-contracts/new/"
    page = support_staff_client.get(url)
    response = support_staff_client.post(
        url,
        {
            "once_token": page.context["once_token"],
            "concluded_on": "2025-03-15",
            "starts_on": "2025-04-01",
            "ends_on": "",
            "uploads": [pdf("契約書.pdf")],
        },
    )
    assert response.status_code == 302
    contract = SupportContract.objects.get()
    assert contract.attachments.get().kind == AttachmentKind.SUPPORT_CONTRACT
    response = submit(
        support_staff_client,
        url,
        {"concluded_on": "2026-03-15", "starts_on": "2026-04-01", "ends_on": ""},
    )
    assert "starts_on" in response.context["form"].errors


def test_add_and_void_attachments(support_staff_client: Client) -> None:
    host = make_host()
    contract = SupportContract.objects.create(
        host_organization=host, concluded_on=date(2025, 3, 1), starts_on=date(2025, 4, 1)
    )
    url = f"/hosts/{host.pk}/support-contracts/{contract.pk}/attachments/new/"
    page = support_staff_client.get(url)
    response = support_staff_client.post(
        url, {"once_token": page.context["once_token"], "uploads": [pdf("契約書.pdf")]}
    )
    assert response.status_code == 302
    attachment = contract.attachments.get()

    void_url = f"/hosts/{host.pk}/attachments/{attachment.pk}/void/"
    response = submit(support_staff_client, void_url, {"reason": ""})
    assert "reason" in response.context["form"].errors
    response = submit(support_staff_client, void_url, {"reason": "別の会社の契約書"})
    assert response.status_code == 302
    attachment.refresh_from_db()
    assert attachment.voided_at is not None and attachment.void_reason == "別の会社の契約書"
    # 取り消した添付・ほかの受入れ機関の添付は対象にしない
    assert support_staff_client.get(void_url).status_code == 404
    other = make_host(name="別の会社", corporate_number=VALID_NUMBERS[1])
    assert (
        support_staff_client.get(f"/hosts/{other.pk}/attachments/{attachment.pk}/void/").status_code
        == 404
    )
    assert (
        support_staff_client.get(
            f"/hosts/{host.pk}/unknown/{contract.pk}/attachments/new/"
        ).status_code
        == 404
    )


def test_second_submission_of_the_same_form_is_ignored(support_staff_client: Client) -> None:
    page = support_staff_client.get("/hosts/new/")
    data = {"once_token": page.context["once_token"], **HOST_DATA}
    assert support_staff_client.post("/hosts/new/", data).status_code == 302
    assert support_staff_client.post("/hosts/new/", data).status_code == 302
    assert HostOrganization.objects.count() == 1
