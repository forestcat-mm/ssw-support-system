"""雇用条件のひな形（T41、ADR-0034）。

- 受入れ機関ごとに、版と同じ項目（手当・控除の行を含む）の値と、外国人ごとに入れる項目の印を持つ。
- 契約の登録でひな形を選ぶと、印の無い項目に値が入り、印の付いた項目は空で画面の先頭に出る。
- ひな形を直しても、作成済みの契約の版は変わらない。版の保存・検査・届出は今のとおり。
"""

from typing import Any

import pytest
from django.test import Client

from apps.contracts.models import EmploymentConditionTemplate, EmploymentContract
from apps.core.models import AuditAction, AuditLog
from apps.hosts.models import EntityType
from apps.workers.models import ForeignWorker
from services import condition_templates
from tests.contracts.factories import site_with_categories, version_values
from tests.contracts.test_contract_screens import first_version_data, rows, submit, to_post
from tests.core.factories import make_user, verified_client
from tests.hosts.factories import category, make_host, make_site
from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db

MARKED = ["base_wage", "starts_on", "ends_on", "allowances"]


def template_data(host_site: Any, **overrides: Any) -> dict[str, Any]:
    values = version_values(host_site, renewal_criteria=["workload"]) | {
        "categories": [category("machine_metal_processing")]
    }
    for name in ("changed_on", "starts_on", "ends_on", "base_wage"):
        values.pop(name)
    return (
        to_post(values)
        | rows("shifts", [])
        | rows("allowances", [{"name": "住宅手当", "amount": 10000}])
        | rows("deductions", [{"kind": "housing", "amount": 30000}])
        | {"name": "製造ライン 正社員", "is_active": "on", "marked": MARKED}
        | overrides
    )


def make_template(client: Client, site: Any) -> EmploymentConditionTemplate:
    url = f"/hosts/{site.host_organization_id}/condition-templates/new/"
    response = submit(client, url, template_data(site))
    assert response.status_code == 302, response.context["editor"].form.errors
    return EmploymentConditionTemplate.objects.get()


def test_template_keeps_the_values_rows_and_marks(support_staff_client: Client) -> None:
    site = site_with_categories()
    template = make_template(support_staff_client, site)
    assert template.host_organization == site.host_organization
    assert template.values["site"] == site.pk
    assert template.values["wage_closing_day"] == "毎月末日"  # 締切日・支払日もひな形に
    assert template.values["wage_payment_day"] == "翌月25日"
    assert template.values["start_time"] == "08:30"
    assert "base_wage" not in template.values and "changed_on" not in template.values
    assert template.details["deductions"] == [{"kind": "housing", "name": "", "amount": 30000}]
    assert template.marked == sorted(MARKED)
    assert AuditLog.objects.filter(
        action=AuditAction.CREATE, content_type__model="employmentconditiontemplate"
    ).exists()
    detail = support_staff_client.get(f"/hosts/{site.host_organization_id}/").content.decode()
    assert f'data-template="{template.pk}"' in detail


def test_registration_fills_in_the_unmarked_items(support_staff_client: Client) -> None:
    site = site_with_categories()
    template = make_template(support_staff_client, site)
    worker = make_worker()
    url = f"/workers/{worker.pk}/contracts/new/"
    page = support_staff_client.get(f"{url}?template={template.pk}")
    content = page.content.decode()
    editor = page.context["editor"]
    assert 'id="template-marked"' in content and 'id="template-marked-details"' in content
    assert editor.marked == ["base_wage", "ends_on", "starts_on"]
    assert editor.form.initial["duties"] == "機械金属加工の作業"
    assert "base_wage" not in editor.form.initial
    assert editor.allowances.initial is None  # 印の付いた行は入れない
    assert editor.deductions.initial == [{"kind": "housing", "name": "", "amount": 30000}]
    # 保存は今までどおり（印の項目を入れて保存）
    data = first_version_data(site) | {"template": str(template.pk)}
    response = submit(support_staff_client, url, data)
    assert response.status_code == 302
    version = EmploymentContract.objects.get(worker=worker).versions.get()
    assert version.base_wage == 220000 and version.duties == "機械金属加工の作業"
    # ひな形を直しても、作成済みの版は変わらない
    template_url = f"/hosts/{site.host_organization_id}/condition-templates/{template.pk}/"
    response = submit(
        support_staff_client, template_url, template_data(site, duties="別の作業", marked=[])
    )
    assert response.status_code == 302
    template.refresh_from_db()
    assert template.values["duties"] == "別の作業" and template.marked == []
    version.refresh_from_db()
    assert version.duties == "機械金属加工の作業"


def test_templates_not_in_use_are_not_offered(support_staff_client: Client) -> None:
    site = site_with_categories()
    template = make_template(support_staff_client, site)
    condition_templates.set_active(template, False, by=None)
    worker: ForeignWorker = make_worker()
    page = support_staff_client.get(f"/workers/{worker.pk}/contracts/new/?template={template.pk}")
    assert page.context["editor"].template is None
    assert 'id="condition-template-select"' not in page.content.decode()


def test_site_and_marks_are_checked(support_staff_client: Client) -> None:
    site = site_with_categories()
    other = make_site(
        make_host(name="別の会社", entity_type=EntityType.SOLE_PROPRIETOR, corporate_number="")
    )
    url = f"/hosts/{site.host_organization_id}/condition-templates/new/"
    response = submit(support_staff_client, url, template_data(site, **{"site": str(other.pk)}))
    assert response.status_code == 200  # ほかの受入れ機関の事業所は選べない
    response = submit(support_staff_client, url, template_data(site, marked=["changed_on"]))
    assert response.status_code == 200 and not EmploymentConditionTemplate.objects.exists()


def test_permissions() -> None:
    site = site_with_categories()
    viewer = verified_client(make_user("viewer41", roles=()))
    url = f"/hosts/{site.host_organization_id}/condition-templates/new/"
    assert viewer.get(url).status_code == 403
