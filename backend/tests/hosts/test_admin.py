"""受入れ機関の admin（2 社・各 1 事業所の登録、加入状況・協力確認書・契約、権限、監査ログ）。"""

import re
from datetime import date
from typing import Any

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from apps.core.models import AttachmentKind, AuditAction, AuditLog, User
from apps.hosts.models import HostMembership, HostOrganization, HostSite, SupportContract
from tests.conftest import PDF_BYTES
from tests.core.factories import make_user, verified_client

from .factories import VALID_NUMBERS, category, field, make_host

pytestmark = pytest.mark.django_db

HOST_URL = "/admin/hosts/hostorganization/"
SITE_URL = "/admin/hosts/hostsite/"
# インラインと、既存の行を送り直すときの編集可能な列（事業所は一覧のみで編集しない）
INLINES: dict[str, tuple[str, ...]] = {
    "memberships": ("field", "joined_on", "valid_until", "member_number"),
    "cooperation_confirmations": ("municipality", "submitted_on"),
    "support_contracts": ("concluded_on", "starts_on", "ends_on", "scope"),
    "sites": (),
}


def _errors(response: Any) -> list[str]:
    """フォームのエラー文だけを取り出す（失敗時の表示用）。"""
    html = response.content.decode()
    return [
        re.sub(r"<[^>]+>", " ", block).strip()
        for block in re.findall(r'<ul class="errorlist[^"]*"[^>]*>(.*?)</ul>', html, re.S)
    ]


def _host_form(
    host: HostOrganization | None = None, **rows: list[dict[str, Any]]
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "entity_type": "corporation",
        "name": "テスト工業株式会社",
        "name_kana": "テストコウギョウ",
        "corporate_number": VALID_NUMBERS[0],
        "representative_title": "代表取締役",
        "representative_name": "山田 太郎",
        "postal_code": "100-0001",
        "address": "東京都千代田区千代田1-1",
        "phone_number": "03-0000-0000",
        "is_active": "on",
    }
    for prefix, editable in INLINES.items():
        existing = list(getattr(host, prefix).order_by("pk")) if host else []
        new = rows.get(prefix, [])
        all_rows = [
            {"id": r.pk} | {name: _value(getattr(r, name)) for name in editable} for r in existing
        ] + new
        data |= {
            f"{prefix}-TOTAL_FORMS": str(len(all_rows)),
            f"{prefix}-INITIAL_FORMS": str(len(existing)),
            f"{prefix}-MIN_NUM_FORMS": "0",
            f"{prefix}-MAX_NUM_FORMS": "1000",
        }
        for i, row in enumerate(all_rows):
            data |= {f"{prefix}-{i}-{k}": v for k, v in row.items()}
    return data


def _value(value: Any) -> Any:
    if value is None:
        return ""
    return getattr(value, "pk", value)


def _site_form(
    host: HostOrganization, field_code: str, category_codes: list[str], **overrides: Any
) -> dict[str, Any]:
    return {
        "host_organization": host.pk,
        "name": "本社工場",
        "postal_code": "100-0001",
        "address": "東京都千代田区千代田1-1",
        "industry_code": "2194" if field_code == "industrial_products" else "09",
        "field": field(field_code).pk,
        "categories": [category(code).pk for code in category_codes],
        "is_active": "on",
    } | overrides


def test_register_two_hosts_with_one_site_each(admin_client: Client) -> None:
    """T08 の完了条件: 2 社・各 1 事業所を登録できる。"""
    industrial = _host_form()
    food = _host_form(
        memberships=[{"field": field("food_manufacturing").pk, "joined_on": "2025-04-01"}],
        support_contracts=[
            {"concluded_on": "2025-03-15", "starts_on": "2025-04-01", "scope": "full"}
        ],
        cooperation_confirmations=[{"municipality": "横浜市", "submitted_on": "2025-04-10"}],
    ) | {"name": "テスト食品株式会社", "corporate_number": "7999-9000-00010"}
    for data in (industrial, food):
        response = admin_client.post(f"{HOST_URL}add/", data)
        assert response.status_code == 302, _errors(response)
    a, b = HostOrganization.objects.order_by("pk")
    assert b.corporate_number == "7999900000010"  # 正規化して保存
    assert b.memberships.get().field.code == "food_manufacturing"
    assert b.support_contracts.count() == 1 and b.cooperation_confirmations.count() == 1

    for host, field_code, codes in (
        (a, "industrial_products", ["machine_metal_processing", "sewing"]),
        (b, "food_manufacturing", ["food_manufacturing_general"]),
    ):
        response = admin_client.post(f"{SITE_URL}add/", _site_form(host, field_code, codes))
        assert response.status_code == 302, _errors(response)
    assert HostSite.objects.count() == 2
    assert set(a.sites.get().categories.values_list("name", flat=True)) == {"機械金属加工", "縫製"}
    assert admin_client.get(f"{HOST_URL}{b.pk}/change/").status_code == 200


def test_invalid_corporate_number_is_rejected(admin_client: Client) -> None:
    response = admin_client.post(
        f"{HOST_URL}add/", _host_form() | {"corporate_number": "7000012050003"}
    )
    assert response.status_code == 200
    assert "検査用数字が一致しません" in response.content.decode()
    assert not HostOrganization.objects.exists()


def test_sole_proprietor_without_number_and_corporation_without_number(
    admin_client: Client,
) -> None:
    response = admin_client.post(f"{HOST_URL}add/", _host_form() | {"corporate_number": ""})
    assert response.status_code == 200
    assert "法人の場合は法人番号を入力してください" in response.content.decode()
    data = _host_form() | {
        "corporate_number": "",
        "entity_type": "sole_proprietor",
        "name": "山田製作所",
    }
    assert admin_client.post(f"{HOST_URL}add/", data).status_code == 302


def test_site_category_from_another_field_is_rejected(admin_client: Client) -> None:
    host = make_host()
    response = admin_client.post(
        f"{SITE_URL}add/", _site_form(host, "industrial_products", ["fishery_processing"])
    )
    assert response.status_code == 200
    assert "事業所の分野に属さない業務区分です" in response.content.decode()


def test_overlapping_contract_is_rejected_in_admin(admin_client: Client) -> None:
    host = make_host()
    SupportContract.objects.create(
        host_organization=host, concluded_on=date(2025, 3, 1), starts_on=date(2025, 4, 1)
    )
    data = _host_form(
        host,
        support_contracts=[
            {"concluded_on": "2026-03-01", "starts_on": "2026-04-01", "scope": "full"}
        ],
    )
    response = admin_client.post(f"{HOST_URL}{host.pk}/change/", data)
    assert response.status_code == 200
    assert any("期間が重なっています" in e for e in _errors(response)), _errors(response)
    assert host.support_contracts.count() == 1


def test_membership_certificate_upload_limits_kinds(admin_client: Client, admin_user: User) -> None:
    host = make_host()
    membership = HostMembership.objects.create(
        host_organization=host, field=field("food_manufacturing"), joined_on=date(2025, 4, 1)
    )
    prefix = "core-attachment-content_type-object_id"
    page = admin_client.get(f"/admin/hosts/hostmembership/{membership.pk}/change/").content.decode()
    assert AttachmentKind.MEMBERSHIP_CERTIFICATE.value in page
    assert AttachmentKind.SUPPORT_CONTRACT.value not in page

    def post(kind: str) -> Any:
        return admin_client.post(
            f"/admin/hosts/hostmembership/{membership.pk}/change/",
            {
                "host_organization": host.pk,
                "field": membership.field_id,
                "joined_on": "2025-04-01",
                f"{prefix}-TOTAL_FORMS": "1",
                f"{prefix}-INITIAL_FORMS": "0",
                f"{prefix}-MIN_NUM_FORMS": "0",
                f"{prefix}-MAX_NUM_FORMS": "1000",
                f"{prefix}-0-kind": kind,
                f"{prefix}-0-file": SimpleUploadedFile(
                    "cert.pdf", PDF_BYTES, content_type="application/pdf"
                ),
            },
        )

    assert post(AttachmentKind.SUPPORT_CONTRACT).status_code == 200  # この画面の種別ではない
    assert not membership.attachments.exists()
    assert post(AttachmentKind.MEMBERSHIP_CERTIFICATE).status_code == 302
    attachment = membership.attachments.get()
    assert attachment.created_by == admin_user

    staff_client = verified_client(make_user("tanto", roles=("support_staff",)))
    assert staff_client.get(f"/attachments/{attachment.pk}/download/").status_code == 200
    assert (
        verified_client(make_user("norole"))
        .get(f"/attachments/{attachment.pk}/download/")
        .status_code
        == 403
    )


def test_viewing_host_detail_is_audited(admin_client: Client, admin_user: User) -> None:
    host = make_host()
    admin_client.get(f"{HOST_URL}{host.pk}/change/")
    log = AuditLog.objects.get(action=AuditAction.VIEW)
    assert log.actor == admin_user
    assert log.content_type == ContentType.objects.get_for_model(HostOrganization)
    assert log.object_id == str(host.pk)
    assert AuditLog.objects.filter(
        action=AuditAction.CREATE, content_type=log.content_type
    ).exists()


def test_support_staff_manages_hosts_but_only_views_masters(support_staff_client: Client) -> None:
    assert support_staff_client.post(f"{HOST_URL}add/", _host_form()).status_code == 302
    host = HostOrganization.objects.get()
    assert support_staff_client.get(f"{HOST_URL}{host.pk}/delete/").status_code == 403
    assert support_staff_client.get("/admin/hosts/specifiedskillfield/").status_code == 200
    assert support_staff_client.get("/admin/hosts/specifiedskillfield/add/").status_code == 403
    assert support_staff_client.get("/admin/hosts/operationcategory/add/").status_code == 403
    assert support_staff_client.get("/admin/core/country/add/").status_code == 403


def test_admin_edits_masters(admin_client: Client) -> None:
    assert admin_client.get("/admin/hosts/specifiedskillfield/add/").status_code == 200
    assert admin_client.get("/admin/core/country/add/").status_code == 200
    assert (
        admin_client.get(
            f"/admin/hosts/specifiedskillfield/{field('food_manufacturing').pk}/delete/"
        ).status_code
        == 403
    )


def test_overlapping_contracts_entered_together_are_rejected_on_add(admin_client: Client) -> None:
    contracts = [
        {"concluded_on": "2025-03-01", "starts_on": "2025-04-01", "scope": "full"},
        {"concluded_on": "2026-03-01", "starts_on": "2026-04-01", "scope": "full"},
    ]
    response = admin_client.post(f"{HOST_URL}add/", _host_form(support_contracts=contracts))
    assert response.status_code == 200
    assert any("重なっています" in e for e in _errors(response)), _errors(response)
    assert not HostOrganization.objects.exists()
    contracts[0]["ends_on"] = "2026-03-31"
    assert (
        admin_client.post(f"{HOST_URL}add/", _host_form(support_contracts=contracts)).status_code
        == 302
    )
