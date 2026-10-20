"""担当者・支援責任者の履歴（T45。ADR-0037）。"""

import importlib
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.test import Client
from django.utils import timezone

from apps.core.models import (
    AuditAction,
    AuditLog,
    Country,
    Office,
    OfficeManagerPeriod,
    SupportLanguage,
)
from apps.workers.models import ForeignWorker, Sex, StaffAssignment
from services import audit, staff_periods, workers
from services.period_rebuild import Change, Period, rebuild
from tests.core.factories import make_office, make_staff
from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db

TOKYO = ZoneInfo("Asia/Tokyo")
TODAY = date(2026, 10, 20)


@pytest.fixture
def worker() -> ForeignWorker:
    worker = make_worker(assigned_staff=make_staff(name="佐藤"))
    StaffAssignment.objects.create(
        worker=worker, staff=worker.assigned_staff, starts_on=date(2026, 4, 1)
    )
    return worker


# --- 組み立て（マイグレーションで使う。T45-D3）-----------------------------------------------


def test_rebuild_from_the_create_and_update_records() -> None:
    periods = rebuild(
        registered_on=date(2026, 4, 1),
        created_with=(1,),
        changes=[
            Change(date(2026, 6, 1), 1, 2),
            Change(date(2026, 8, 1), 2, 3),
            Change(date(2026, 8, 1), 3, 4),  # 同じ日はその日の最後の値
        ],
        current=4,
        current_since=date(2026, 8, 1),
    )
    assert periods == [
        Period(1, date(2026, 4, 1), False),
        Period(2, date(2026, 6, 1), False),
        Period(4, date(2026, 8, 1), False),
    ]


def test_rebuild_without_the_create_record_is_estimated() -> None:
    # 変更の記録だけ: 最初の値は変更の前の値（推定）
    assert rebuild(
        registered_on=date(2026, 4, 1),
        created_with=None,
        changes=[Change(date(2026, 6, 1), 1, 2)],
        current=2,
        current_since=date(2026, 6, 1),
    ) == [Period(1, date(2026, 4, 1), True), Period(2, date(2026, 6, 1), False)]
    # 記録が何も無い: 今の担当者 1 行（開始日＝登録の日、推定）
    assert rebuild(
        registered_on=date(2026, 4, 1),
        created_with=None,
        changes=[],
        current=5,
        current_since=date(2026, 9, 1),
    ) == [Period(5, date(2026, 4, 1), True)]
    # 担当者がいない
    assert (
        rebuild(
            registered_on=date(2026, 4, 1),
            created_with=None,
            changes=[],
            current=None,
            current_since=date(2026, 9, 1),
        )
        == []
    )


def test_rebuild_adds_an_estimated_row_when_the_log_misses_a_change() -> None:
    assert rebuild(
        registered_on=date(2026, 4, 1),
        created_with=(1,),
        changes=[],
        current=2,  # 監査ログの外で変わった
        current_since=date(2026, 9, 1),
    ) == [Period(1, date(2026, 4, 1), False), Period(2, date(2026, 9, 1), True)]


def test_migration_rebuilds_the_rows_from_the_audit_log() -> None:
    first, second = make_staff(name="佐藤"), make_staff(name="鈴木")
    logged = make_worker(name_romaji="LOGGED", assigned_staff=first)  # 作成の記録が残る
    logged.assigned_staff = second
    logged.save()  # 変更の記録が残る
    with audit.suppressed():  # 監査ログの無い外国人（記録を残す前に登録した人）
        silent = make_worker(name_romaji="SILENT", assigned_staff=first)
    StaffAssignment.objects.all().delete()
    migration = importlib.import_module("apps.workers.migrations.0008_staff_periods")
    migration.rebuild_periods(apps, None)
    today = timezone.localdate()
    # 作成と変更が同じ日なので、その日の最後の値だけ
    [period] = StaffAssignment.objects.filter(worker=logged)
    assert (period.staff, period.starts_on, period.is_estimated) == (second, today, False)
    [estimated] = StaffAssignment.objects.filter(worker=silent)
    assert (estimated.staff, estimated.starts_on, estimated.is_estimated) == (first, today, True)


# --- 担当者を変える（T45-D2・D4）-------------------------------------------------------------


def test_registering_a_worker_starts_the_history() -> None:
    staff = make_staff(name="佐藤")
    worker = ForeignWorker(
        name_romaji="NEW WORKER",
        nationality=Country.objects.get(code="CN"),
        birth_date=date(1998, 5, 1),
        sex=Sex.FEMALE,
        support_language=SupportLanguage.objects.get(code="zh-hans"),
        assigned_staff=staff,
    )
    workers.register_worker(worker, [], by=None)
    [period] = worker.staff_assignments.all()
    assert (period.staff, period.starts_on, period.is_estimated) == (
        staff,
        timezone.localdate(),
        False,
    )


def test_change_keeps_the_history_and_the_current_copy(worker: ForeignWorker) -> None:
    first = worker.assigned_staff
    second = make_staff(name="鈴木")
    staff_periods.change_worker_staff(
        worker, second, starts_on=date(2026, 9, 1), reason="異動", by=None, today=TODAY
    )
    worker.refresh_from_db()
    assert worker.assigned_staff == second
    assert staff_periods.worker_staff_on(worker, date(2026, 8, 31)).staff == first
    assert staff_periods.worker_staff_on(worker, date(2026, 9, 1)).staff == second
    # 最初の行より前は、最初の担当者を推定として返す
    assert staff_periods.worker_staff_on(worker, date(2026, 3, 1)) == staff_periods.StaffOn(
        first, True
    )
    # 監査ログに、行の作成と今の担当者の更新が残る
    assert AuditLog.objects.filter(
        action=AuditAction.CREATE,
        content_type=ContentType.objects.get_for_model(StaffAssignment),
    ).exists()
    assert AuditLog.objects.filter(
        action=AuditAction.UPDATE, object_id=str(worker.pk), changes__has_key="assigned_staff"
    ).exists()


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"starts_on": date(2026, 10, 21)}, "starts_on"),  # 未来
        ({"starts_on": date(2026, 4, 1)}, "starts_on"),  # 今の行の開始日より後でない
        ({"reason": " "}, "reason"),
        ({"same": True}, "staff"),
    ],
)
def test_change_is_checked(worker: ForeignWorker, change: dict[str, Any], field: str) -> None:
    values: dict[str, Any] = {"starts_on": date(2026, 9, 1), "reason": "異動"} | change
    staff = worker.assigned_staff if values.pop("same", False) else make_staff(name="鈴木")
    with pytest.raises(ValidationError) as error:
        staff_periods.change_worker_staff(worker, staff, by=None, today=TODAY, **values)
    assert field in error.value.message_dict


def test_void_goes_back_to_the_previous_staff(worker: ForeignWorker) -> None:
    first = worker.assigned_staff
    wrong = staff_periods.change_worker_staff(
        worker,
        make_staff(name="鈴木"),
        starts_on=date(2026, 9, 1),
        reason="異動",
        by=None,
        today=TODAY,
    )
    with pytest.raises(ValidationError):
        staff_periods.void_worker_period(wrong, by=None, reason="")
    staff_periods.void_worker_period(wrong, by=None, reason="別の外国人と取り違えた")
    worker.refresh_from_db()
    assert worker.assigned_staff == first
    wrong.refresh_from_db()
    assert wrong.voided_at is not None  # 消さずに残る


def test_change_screen_and_history_tab(admin_client: Client, worker: ForeignWorker) -> None:
    edit = admin_client.get(f"/workers/{worker.pk}/edit/")
    assert "assigned_staff" not in edit.context["form"].fields  # 編集の画面からは変えない
    period = worker.staff_assignments.get()
    period.is_estimated = True
    period.save()
    history = admin_client.get(f"/workers/{worker.pk}/history/").content.decode()
    assert 'id="staff-period-table"' in history and 'data-estimated="true"' in history
    page = admin_client.get(f"/workers/{worker.pk}/staff/change/")
    second = make_staff(name="鈴木")
    response = admin_client.post(
        f"/workers/{worker.pk}/staff/change/",
        {
            "staff": str(second.pk),
            "starts_on": timezone.localdate().isoformat(),
            "reason": "異動のため引継ぎ",
            page.context["once_field"]: page.context["once_token"],
        },
    )
    assert response.status_code == 302
    worker.refresh_from_db()
    assert worker.assigned_staff == second
    period = worker.staff_assignments.get(staff=second)
    page = admin_client.get(f"/workers/{worker.pk}/staff/{period.pk}/void/")
    response = admin_client.post(
        f"/workers/{worker.pk}/staff/{period.pk}/void/",
        {"reason": "誤り", page.context["once_field"]: page.context["once_token"]},
    )
    assert response.status_code == 302
    worker.refresh_from_db()
    assert worker.assigned_staff != second


# --- 事務所の支援責任者（T45-D5）-----------------------------------------------------------


@pytest.fixture
def office() -> Office:
    return make_office(name="本社")


def test_office_manager_history(office: Office) -> None:
    first = make_staff(office=office, name="山田", is_support_manager=True)
    second = make_staff(office=office, name="田中", is_support_manager=True)
    elsewhere = make_staff(office=make_office(name="支社"), name="鈴木", is_support_manager=True)
    with pytest.raises(ValidationError) as error:
        staff_periods.change_office_manager(
            office, elsewhere, starts_on=date(2026, 4, 1), reason="就任", by=None, today=TODAY
        )
    assert "staff" in error.value.message_dict
    staff_periods.change_office_manager(
        office, first, starts_on=date(2026, 4, 1), reason="就任", by=None, today=TODAY
    )
    staff_periods.change_office_manager(
        office, second, starts_on=date(2026, 10, 1), reason="交代", by=None, today=TODAY
    )
    office.refresh_from_db()
    assert office.support_manager == second
    periods = staff_periods.office_periods(office)
    assert staff_periods.staff_on(periods, date(2026, 9, 30)).staff == first
    latest = OfficeManagerPeriod.objects.get(office=office, staff=second)
    staff_periods.void_office_period(latest, by=None, reason="日付の誤り")
    office.refresh_from_db()
    assert office.support_manager == first


def test_office_migration_rebuilds_the_manager(office: Office) -> None:
    manager = make_staff(office=office, name="山田", is_support_manager=True)
    office.support_manager = manager
    with audit.suppressed():
        office.save()
    # 変更の記録（監査ログは追記専用なので、指定の日時で足す。事務所の作成のときは空）
    AuditLog.objects.create(
        action=AuditAction.UPDATE,
        content_type=ContentType.objects.get_for_model(Office),
        object_id=str(office.pk),
        changes={"support_manager": [None, manager.pk]},
        occurred_at=timezone.now(),
    )
    migration = importlib.import_module("apps.core.migrations.0037_staff_periods")
    migration.rebuild_periods(apps, None)
    [period] = OfficeManagerPeriod.objects.filter(office=office)
    assert (period.staff, period.starts_on, period.is_estimated) == (
        manager,
        timezone.localdate(),
        False,
    )
