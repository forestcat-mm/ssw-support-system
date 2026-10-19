"""支援計画の画面（T17）: 支援タブ・計画の作成・新版・標準の計画・自機関と担当者の新しい欄。"""

from datetime import date
from typing import Any

import pytest
from django import forms
from django.test import Client

from apps.core.models import AuditAction, AuditLog, Organization
from apps.support.forms import PlanEditor
from apps.support.models import SupportPlan
from services import support_plans, workers
from tests.contracts.factories import make_contract, site_with_categories
from tests.core.factories import make_organization, make_staff, make_user, verified_client
from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db


def post_data(editor: PlanEditor, **overrides: Any) -> dict[str, Any]:
    """編集画面の初期値を、ブラウザが送るのと同じ形の POST にする。"""
    data: dict[str, Any] = {}
    for form in editor.all_forms():
        for bound in form:
            value = bound.value()
            widget = bound.field.widget
            if isinstance(widget, forms.CheckboxInput):
                if value:
                    data[bound.html_name] = "on"
            elif isinstance(value, (list, tuple)):
                data[bound.html_name] = [str(item) for item in value]
            elif value is None:
                data[bound.html_name] = ""
            else:
                data[bound.html_name] = str(getattr(value, "pk", value))
    data.update(overrides)
    return data


@pytest.fixture
def worker() -> Any:
    staff = make_staff(name="佐藤", position="支援担当")
    worker = make_worker(assigned_staff=staff)
    make_contract(worker, site=site_with_categories())
    return worker


def create_plan_via_screen(client: Client, worker: Any, **overrides: Any) -> Any:
    url = f"/workers/{worker.pk}/support/plans/new/"
    page = client.get(url)
    assert page.status_code == 200
    data = post_data(page.context["editor"], **overrides)
    data["once_token"] = page.context["once_token"]
    data["host"] = page.context["selected_host"].pk
    return client.post(url, data)


def test_support_tab_shows_create_button_when_no_plan(
    support_staff_client: Client, worker: Any
) -> None:
    page = support_staff_client.get(f"/workers/{worker.pk}/support/")
    assert page.status_code == 200
    html = page.content.decode()
    assert f"/workers/{worker.pk}/support/plans/new/" in html
    assert f'href="/workers/{worker.pk}/support/"' in html  # 外国人詳細のタブに「支援」


def test_create_plan_from_template_fills_staff_and_language(
    support_staff_client: Client, worker: Any
) -> None:
    page = support_staff_client.get(f"/workers/{worker.pk}/support/plans/new/")
    editor = page.context["editor"]
    assert editor.activity_forms["1.main"]["staff"].value() == worker.assigned_staff_id
    assert editor.item_forms["pre_guidance"]["languages"].value() == [worker.support_language_id]
    response = create_plan_via_screen(support_staff_client, worker)
    plan = SupportPlan.objects.get(worker=worker)
    assert response["Location"] == f"/workers/{worker.pk}/support/?plan={plan.pk}"
    version = support_plans.latest_version(plan)
    assert version is not None and version.version == 1
    assert (
        support_plans.content_of(version).activities["1.main"].staff_id == worker.assigned_staff_id
    )


def test_create_plan_shows_service_errors_under_fields(
    support_staff_client: Client, worker: Any
) -> None:
    response = create_plan_via_screen(
        support_staff_client,
        worker,
        **{"act-2_a-planned": "no", "act-2_a-planned_note": ""},
    )
    assert response.status_code == 200
    html = response.content.decode()
    assert 'id="id_act-2_a-planned_note-error"' in html
    assert 'id="section-2" open' in html  # エラーのある区分は開く
    assert not SupportPlan.objects.filter(worker=worker).exists()


def test_new_version_and_diff_on_tab(support_staff_client: Client, worker: Any) -> None:
    create_plan_via_screen(support_staff_client, worker)
    plan = SupportPlan.objects.get(worker=worker)
    url = f"/workers/{worker.pk}/support/plans/{plan.pk}/versions/new/"
    page = support_staff_client.get(url)
    data = post_data(
        page.context["editor"],
        **{
            "act-6_main-planned_note": "平日9時〜18時に随時",
            "plan-change_reason": "受付時間の見直し",
        },
    )
    data["once_token"] = page.context["once_token"]
    response = support_staff_client.post(url, data)
    assert response.status_code == 302
    tab = support_staff_client.get(response["Location"])
    html = tab.content.decode()
    assert tab.context["version"].version == 2
    assert "平日9時〜18時に随時" in html and 'id="plan-diff"' in html
    labels = [row.label for row in tab.context["diff"]]
    assert any("6" in label and "実施時期" in label for label in labels)


def test_new_version_requires_reason(support_staff_client: Client, worker: Any) -> None:
    create_plan_via_screen(support_staff_client, worker)
    plan = SupportPlan.objects.get(worker=worker)
    url = f"/workers/{worker.pk}/support/plans/{plan.pk}/versions/new/"
    page = support_staff_client.get(url)
    data = post_data(page.context["editor"])
    data["once_token"] = page.context["once_token"]
    response = support_staff_client.post(url, data)
    assert response.status_code == 200
    assert 'id="id_plan-change_reason-error"' in response.content.decode()


def test_warnings_are_shown_without_blocking(support_staff_client: Client) -> None:
    worker = make_worker()  # 担当者なし → 担当の注意が出る
    make_contract(worker, site=site_with_categories())
    page = support_staff_client.get(f"/workers/{worker.pk}/support/plans/new/")
    html = page.content.decode()
    assert "担当者が空です" in html and 'data-warnings="' in html
    response = create_plan_via_screen(support_staff_client, worker)
    assert response.status_code == 302


def test_tab_view_is_audited(support_staff_client: Client, worker: Any) -> None:
    create_plan_via_screen(support_staff_client, worker)
    support_staff_client.get(f"/workers/{worker.pk}/support/")
    entry = AuditLog.objects.filter(action=AuditAction.VIEW, object_id=str(worker.pk)).latest("pk")
    assert entry.context["tab"] == "support"
    assert "support_plan_version" in entry.context["shown"]


def test_deactivated_worker_plan_is_read_only(admin_client: Client, worker: Any) -> None:
    create_plan_via_screen(admin_client, worker)
    plan = SupportPlan.objects.get(worker=worker)
    workers.deactivate(worker, by=None, reason="重複")
    html = admin_client.get(f"/workers/{worker.pk}/support/").content.decode()
    assert "/versions/new/" not in html
    url = f"/workers/{worker.pk}/support/plans/{plan.pk}/versions/new/"
    assert admin_client.get(url).status_code == 403


def test_support_screens_need_permissions(worker: Any) -> None:
    viewer = make_user("viewer", roles=("support_staff",))
    viewer.user_roles.get().role.permissions.filter(action="support.manage").delete()
    client = verified_client(viewer)
    assert client.get(f"/workers/{worker.pk}/support/").status_code == 200
    assert client.get(f"/workers/{worker.pk}/support/plans/new/").status_code == 403


# --- 設定: 標準の支援計画 ------------------------------------------------------------------


def test_standard_plan_is_admin_only(support_staff_client: Client) -> None:
    assert support_staff_client.get("/settings/support-plan/").status_code == 403
    assert support_staff_client.get("/settings/support-plan/edit/").status_code == 403


def test_edit_standard_plan_creates_new_version(admin_client: Client) -> None:
    admin_client.get("/settings/support-plan/")  # 版 1 を作る
    page = admin_client.get("/settings/support-plan/edit/")
    assert "plan-applies_from" not in page.content.decode()  # 標準の計画に適用開始日は無い
    data = post_data(
        page.context["editor"],
        **{"act-9_main-planned_note": "毎月1回", "plan-change_reason": "面談を毎月にする"},
    )
    data["once_token"] = page.context["once_token"]
    response = admin_client.post("/settings/support-plan/edit/", data)
    assert response["Location"] == "/settings/support-plan/"
    plan = support_plans.template_plan()
    assert plan.versions.count() == 2
    assert support_plans.template_content().activities["9.main"].planned_note == "毎月1回"
    html = admin_client.get("/settings/support-plan/").content.decode()
    assert 'id="plan-diff"' in html


# --- 自機関の 3 項目・担当者の役職 ----------------------------------------------------------


def test_organization_and_staff_new_fields(admin_client: Client) -> None:
    make_organization()
    url = "/settings/organization/edit/"
    page = admin_client.get(url)
    form = page.context["form"]
    data = {name: form[name].value() or "" for name in form.fields}
    data |= {
        "once_token": page.context["once_token"],
        "registered_on": "2019-06-01",
        "representative_name": "山田 一郎",
        "representative_name_kana": "やまだ いちろう",
        "phone_number": "03-0000-0000",
    }
    admin_client.post(url, data)
    org = Organization.objects.get()
    assert org.registered_on == date(2019, 6, 1)
    assert (org.representative_name, org.representative_name_kana) == (
        "山田 一郎",
        "やまだ いちろう",
    )
    html = admin_client.get("/settings/organization/").content.decode()
    assert "2019-06-01" in html and "やまだ いちろう" in html

    staff = make_staff(name="鈴木")
    page = admin_client.get(f"/settings/staff/{staff.pk}/")
    form = page.context["form"]
    data = {name: form[name].value() for name in form.fields}
    data = {k: v for k, v in data.items() if v not in (None, False, [])}
    data |= {
        "once_token": page.context["once_token"],
        "position": "支援責任者",
        "is_support_staff": "on",
    }
    data["is_active"] = "on"
    admin_client.post(f"/settings/staff/{staff.pk}/", data)
    staff.refresh_from_db()
    assert staff.position == "支援責任者"
    assert "支援責任者" in admin_client.get("/settings/staff/").content.decode()


def test_keepalive_extends_session(support_staff_client: Client) -> None:
    response = support_staff_client.post("/accounts/keepalive/")
    assert response.status_code == 204
    # ログインしていなければ 401（ログインの画面に移さない。fetch が成功と取り違えるため。J8）
    assert Client().post("/accounts/keepalive/").status_code == 401
