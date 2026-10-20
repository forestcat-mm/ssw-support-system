"""受入れ機関・事業所・自機関の一括申請の項目（T46b。ADR-0038）。"""

from typing import Any

import pytest
from django.test import Client

from apps.core.models import AuditAction, AuditLog, Organization, OrganizationOfficer
from apps.hosts.models import HostDeclaration, HostFinancialYear, HostOfficer
from services import host_declarations
from tests.core.factories import make_organization, make_staff
from tests.hosts.factories import make_host, make_site
from tests.hosts.test_host_edit_screens import submit

pytestmark = pytest.mark.django_db


def test_host_industry_and_capital(support_staff_client: Client) -> None:
    host = make_host()
    page = support_staff_client.get(f"/hosts/{host.pk}/edit/")
    form = page.context["form"]
    data = {name: form[name].value() or "" for name in form.fields}
    data |= {"industry_1": "0920", "capital": "30000000"}  # CC13 の値はツールのコード
    choices = dict(form.fields["industry_1"].choices)
    data["industry_1"] = next(code for code in choices if code)
    response = submit(support_staff_client, f"/hosts/{host.pk}/edit/", data)
    assert response.status_code == 302, response.context["form"].errors
    host.refresh_from_db()
    assert host.capital == 30_000_000 and host.industry_1 == data["industry_1"]
    detail = support_staff_client.get(f"/hosts/{host.pk}/").content.decode()
    assert 'id="host-industries"' in detail and choices[data["industry_1"]] in detail


def test_site_insurance_numbers_are_checked(support_staff_client: Client) -> None:
    host = make_host()
    site = make_site(host)
    url = f"/hosts/{host.pk}/sites/{site.pk}/edit/"
    page = support_staff_client.get(url)
    form = page.context["form"]
    data: dict[str, Any] = {name: form[name].value() for name in form.fields}
    data = {k: ("" if v is None else v) for k, v in data.items()}
    data["categories"] = [c.pk for c in site.categories.all()] or [
        form.fields["categories"].queryset.filter(field=site.field).first().pk
    ]
    data |= {"employment_insurance_number": "1234-567890-1", "labour_insurance_number": "12345"}
    response = submit(support_staff_client, url, data)
    assert "labour_insurance_number" in response.context["form"].errors
    data |= {"labour_insurance_number": "13-1-23-456789-000", "full_time_employees": "25"}
    response = submit(support_staff_client, url, data)
    assert response.status_code == 302, response.context["form"].errors
    site.refresh_from_db()
    assert site.employment_insurance_number == "12345678901"
    assert site.labour_insurance_number == "13123456789000"
    assert site.full_time_employees == 25


def test_financial_years_are_added_and_voided(support_staff_client: Client) -> None:
    host = make_host()
    url = f"/hosts/{host.pk}/application/financial-years/new/"
    response = submit(
        support_staff_client,
        url,
        {"fiscal_year_end": "2026-03-31", "sales": "300000000", "ordinary_profit": "-1200000"},
    )
    assert response.status_code == 302
    row = HostFinancialYear.objects.get(host_organization=host)
    assert row.ordinary_profit == -1_200_000
    # 決算は編集しない（取り消して入れ直す）
    assert (
        support_staff_client.get(
            f"/hosts/{host.pk}/application/financial-years/{row.pk}/"
        ).status_code
        == 404
    )
    void = f"/hosts/{host.pk}/application/financial-years/{row.pk}/void/"
    assert submit(support_staff_client, void, {"reason": "年度の誤り"}).status_code == 302
    row.refresh_from_db()
    assert row.voided_at is not None


def test_declaration_versions_keep_the_previous_one(support_staff_client: Client) -> None:
    host = make_host()
    url = f"/hosts/{host.pk}/application/declarations/new/"
    page = support_staff_client.get(url)
    form = page.context["form"]
    assert form["answer_11"].value() == "no" and form["answer_23"].value() == "yes"  # 通常の答え
    data: dict[str, Any] = {"declared_on": "2026-10-01"}
    for item in host_declarations.items():
        data[f"answer_{item.key}"] = item.expected
    data["answer_11"] = "yes"  # 通常と違う答えには内容が要る
    response = submit(support_staff_client, url, data)
    assert "detail_11" in response.context["form"].errors
    data |= {
        "detail_11": "2025年に残業代の未払いで是正勧告を受け、支払い済み。",
        "missing_ssw": "0",
    }
    assert submit(support_staff_client, url, data).status_code == 302
    first = HostDeclaration.objects.get(host_organization=host)
    assert first.answers["11"]["answer"] == "yes"
    assert [item.key for item in host_declarations.unusual(first)] == ["11"]
    # 新しい版は前の版を初期値にし、前の版は残る
    page = support_staff_client.get(url)
    assert page.context["form"]["answer_11"].value() == "yes"
    data |= {"declared_on": "2026-10-15", "answer_11": "no", "detail_11": ""}
    assert submit(support_staff_client, url, data).status_code == 302
    assert HostDeclaration.objects.filter(host_organization=host).count() == 2
    latest = host_declarations.latest(host)
    assert latest is not None and str(latest.declared_on) == "2026-10-15"


def test_officers_and_applicant_contacts(support_staff_client: Client) -> None:
    host = make_host()
    url = f"/hosts/{host.pk}/application/officers/new/"
    data = {"name": "山田 太郎", "name_kana": "やまだ たろう", "position": "代表取締役"}
    assert submit(support_staff_client, url, data).status_code == 302
    officer = HostOfficer.objects.get(host_organization=host)
    edit = f"/hosts/{host.pk}/application/officers/{officer.pk}/"
    data |= {"appointed_on": "2020-04-01", "resigned_on": "2019-03-31"}
    assert "resigned_on" in submit(support_staff_client, edit, data).context["form"].errors
    data["resigned_on"] = "2026-06-30"
    assert submit(support_staff_client, edit, data).status_code == 302
    officer.refresh_from_db()
    assert str(officer.resigned_on) == "2026-06-30"
    assert AuditLog.objects.filter(action=AuditAction.UPDATE, object_id=str(officer.pk)).exists()
    contact = f"/hosts/{host.pk}/application/applicant-contacts/new/"
    response = submit(
        support_staff_client,
        contact,
        {"name": "佐藤 花子", "relationship": "所属機関の職員（申請担当）", "is_active": "on"},
    )
    assert response.status_code == 302
    detail = support_staff_client.get(f"/hosts/{host.pk}/").content.decode()
    assert (
        'id="officers"' in detail and 'id="applicant-contacts"' in detail and "佐藤 花子" in detail
    )


def test_own_organization_numbers_officers_and_staff_flag(admin_client: Client) -> None:
    make_organization()
    page = admin_client.get("/settings/organization/edit/")
    form = page.context["form"]
    data = {name: form[name].value() or "" for name in form.fields}
    data |= {"corporate_number": "1234567890123"}
    response = submit(admin_client, "/settings/organization/edit/", data)
    assert "corporate_number" in response.context["form"].errors  # 検査数字が合わない
    org = Organization.objects.get()
    response = submit(
        admin_client,
        "/settings/organization/officers/new/",
        {"name": "鈴木 花子", "position": "代表理事"},
    )
    assert response.status_code == 302
    officer = OrganizationOfficer.objects.get(organization=org)
    settings_page = admin_client.get("/settings/organization/").content.decode()
    assert 'id="officers"' in settings_page and "鈴木 花子" in settings_page
    void = f"/settings/organization/officers/{officer.pk}/void/"
    assert submit(admin_client, void, {"reason": "重複"}).status_code == 302
    staff = make_staff()
    page = admin_client.get(f"/settings/staff/{staff.pk}/")
    assert "is_online_application_user" in page.context["form"].fields
