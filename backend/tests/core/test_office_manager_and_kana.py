"""事務所の支援責任者（第1-17号 Ⅲ欄７。修正 1 回目 A5）と、外国人の氏名のカナ（A4）。"""

import datetime

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.core.models import Office
from apps.workers.models import ForeignWorker
from services import form_checks, form_data_1_17, organization, worker_list, workers
from services.worker_list import WorkerListQuery
from tests.core.factories import make_office, make_staff, make_user, verified_client
from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db


def today() -> datetime.date:
    return timezone.localdate()


@pytest.fixture
def office() -> Office:
    return make_office(name="本社")


def test_office_manager_is_chosen_from_its_active_managers(office: Office) -> None:
    manager = make_staff(office=office, name="山田", is_support_manager=True)
    make_staff(office=office, name="佐藤")  # 支援担当者だけ
    make_staff(office=office, name="田中", is_support_manager=True, is_active=False)
    other_office = make_office(name="支社")
    elsewhere = make_staff(office=other_office, name="鈴木", is_support_manager=True)
    assert list(organization.manager_choices(office)) == [manager]
    organization.validate_support_manager(office, manager)
    with pytest.raises(ValidationError):
        organization.validate_support_manager(office, elsewhere)
    # 選んだ人が無効になれば、支援責任者は無いものとみなす
    office.support_manager = manager
    office.save()
    assert organization.support_manager(office) == manager
    manager.is_active = False
    manager.save()
    assert organization.support_manager(office) is None


def test_form_1_17_uses_the_office_manager_only(office: Office) -> None:
    staff = make_staff(office=office, name="佐藤")
    make_staff(office=make_office(name="支社"), name="鈴木", is_support_manager=True)
    worker = make_worker(assigned_staff=staff)
    assert form_data_1_17.office_and_managers(worker) == (office, [])  # ほかの事務所から選ばない
    manager = make_staff(office=office, name="山田", is_support_manager=True)
    office.support_manager = manager
    office.save()
    assert form_data_1_17.office_and_managers(worker) == (office, [manager])
    nobody = make_worker(name_romaji="NO STAFF")
    assert form_data_1_17.office_and_managers(nobody) == (None, [])


def test_missing_manager_points_to_the_office_screen(office: Office) -> None:
    worker = make_worker(assigned_staff=make_staff(office=office, name="佐藤"))
    context = form_checks.CheckContext(
        worker=worker, target=worker, basis_on=worker.birth_date, data={}, template=None
    )
    issues = form_checks.CHECKS["organization_for_plan"].function(context, {})
    missing = next(issue for issue in issues if issue.code == "manager_missing")
    assert missing.destination is not None
    assert (missing.destination.url_name, dict(missing.destination.kwargs)) == (
        "settings:office_edit",
        {"pk": office.pk},
    )


def test_office_screen_offers_only_its_managers(office: Office) -> None:
    """支援責任者は事務所の編集の欄ではなく「支援責任者を変える」で選ぶ（履歴。T45-D5）。"""
    manager = make_staff(office=office, name="山田", is_support_manager=True)
    client = verified_client(make_user("kanri", roles=("admin",)))
    edit = client.get(f"/settings/offices/{office.pk}/")
    assert "support_manager" not in edit.context["form"].fields
    assert 'id="manager-periods"' in edit.content.decode()
    page = client.get(f"/settings/offices/{office.pk}/manager/")
    choices = list(page.context["form"].fields["staff"].queryset)
    assert choices == [manager]
    data = {
        "staff": str(manager.pk),
        "starts_on": today().isoformat(),
        "reason": "支援責任者の就任",
        page.context["once_field"]: page.context["once_token"],
    }
    response = client.post(f"/settings/offices/{office.pk}/manager/", data)
    assert response.status_code == 302
    office.refresh_from_db()
    assert office.support_manager == manager
    [period] = office.manager_periods.all()
    assert (period.staff, period.starts_on, period.is_estimated) == (manager, today(), False)


# --- カナ（A4）---------------------------------------------------------------------------


def test_kana_is_normalized_and_checked() -> None:
    assert workers.clean_name_kana(" やまだ　たろう ") == "ヤマダ　タロウ"
    assert workers.clean_name_kana("ｸﾞｴﾝ ｳﾞｧﾝ") == "グエン　ヴァン"
    assert workers.clean_name_kana("") == ""
    with pytest.raises(ValidationError):
        workers.clean_name_kana("NGUYEN")


def test_kana_is_searchable_with_hiragana() -> None:
    worker = make_worker(name_romaji="NGUYEN VAN AN", name_kana="グエン　ヴァン　アン")
    make_worker(name_romaji="OTHER", name_kanji="")
    admin = make_user("kanri", roles=("admin",))
    found = worker_list.list_workers(
        admin, WorkerListQuery(text="ぐえん ゔぁん", statuses=()), today()
    )
    assert [row.worker for row in found.rows] == [worker]
    assert ForeignWorker.objects.filter(name_kana__startswith="グエン").count() == 1
