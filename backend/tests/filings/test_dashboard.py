"""期限ダッシュボードと届出タスクの処理（T14）。"""

from datetime import date, timedelta
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.test import Client
from django.utils import timezone
from pytest_django.fixtures import Settings

from apps.contracts.models import EmploymentContract
from apps.core.models import AuditAction, AuditLog, User
from apps.filings.models import Filing, FilingKind, FilingStatus, Trigger
from apps.workers.models import ForeignWorker, WorkerStatus
from services import contracts, dashboard, deadlines, filings, workers
from services.dashboard import DashboardQuery
from tests.contracts.factories import make_contract, site_with_categories
from tests.core.factories import make_organization, make_staff, make_user, verified_client
from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db


def today() -> date:
    return timezone.localdate()


def task(
    target: object,
    kind: FilingKind = FilingKind.CONTRACT_CHANGE,
    days_ago: int = 0,
    **kwargs: object,
) -> Filing:
    filing, _created = filings.create(
        kind,
        target=target,  # type: ignore[arg-type]
        basis_on=today() - timedelta(days=days_ago),
        trigger=Trigger.MANUAL,
        **kwargs,  # type: ignore[arg-type]
    )
    return filing


@pytest.fixture
def contract() -> EmploymentContract:
    return make_contract(make_worker(), site=site_with_categories())


# --- サービス ---------------------------------------------------------------------


def test_dashboard_lists_open_tasks_by_due_date_with_counts(contract: EmploymentContract) -> None:
    overdue = task(contract, days_ago=20)  # 随時届出は 14 日以内 → 超過
    soon = task(contract, FilingKind.CONTRACT_TERMINATION, days_ago=10)  # あと 4 日
    submitted = task(contract, FilingKind.CONTRACT_NEW, days_ago=1)
    filings.transition(submitted, FilingStatus.SUBMITTED, by=None, submitted_on=today())
    result = dashboard.dashboard(DashboardQuery(), today())
    assert [row.filing for row in result.rows] == [overdue, soon]
    assert result.level_counts[deadlines.Level.OVERDUE] == 1
    assert result.level_counts[deadlines.Level.WITHIN_7] == 1
    assert result.awaiting_receipt == 1
    # レベルで絞っても件数は変わらない（押して切り替えるため）
    filtered = dashboard.dashboard(DashboardQuery(level=deadlines.Level.WITHIN_7), today())
    assert [row.filing for row in filtered.rows] == [soon]
    assert filtered.level_counts[deadlines.Level.OVERDUE] == 1
    awaiting = dashboard.dashboard(DashboardQuery(statuses=dashboard.AWAITING_RECEIPT), today())
    assert [row.filing for row in awaiting.rows] == [submitted]


def test_dashboard_filters_by_assignee_kind_and_filer(contract: EmploymentContract) -> None:
    sato = make_staff(name="佐藤")
    mine = task(contract, assignee=sato)
    other = task(contract, FilingKind.CONTRACT_TERMINATION)
    organization = make_organization()
    internal = task(organization, FilingKind.REGISTRATION_RENEWAL)
    assert [
        r.filing for r in dashboard.dashboard(DashboardQuery(assignee_id=sato.pk), today()).rows
    ] == [mine]
    unassigned = dashboard.dashboard(DashboardQuery(unassigned_only=True), today()).rows
    assert {r.filing for r in unassigned} == {other, internal}
    by_kind = dashboard.dashboard(DashboardQuery(kind=FilingKind.CONTRACT_TERMINATION), today())
    assert [r.filing for r in by_kind.rows] == [other]
    filer = filings.definition(FilingKind.REGISTRATION_RENEWAL).filer  # 登録支援機関
    by_filer = dashboard.dashboard(DashboardQuery(filer=filer), today())
    assert [r.filing for r in by_filer.rows] == [internal]


def test_dashboard_excludes_deactivated_workers(contract: EmploymentContract) -> None:
    task(contract)
    version = contracts.latest_version(contract)
    assert version is not None
    other = make_contract(make_worker(name_romaji="OTHER", name_kanji=""), site=version.site)
    kept = task(other)
    workers.deactivate(contract.worker, by=None, reason="重複")
    rows = dashboard.dashboard(DashboardQuery(statuses=()), today()).rows
    assert [row.filing for row in rows] == [kept]


def test_target_links(contract: EmploymentContract) -> None:
    worker_task = task(contract)
    host_task = task(contract.host_organization, FilingKind.SUPPORT_DIFFICULTY)
    org_task = task(make_organization(), FilingKind.REGISTRATION_RENEWAL)
    rows = {row.filing: row for row in dashboard.dashboard(DashboardQuery(), today()).rows}
    assert (rows[worker_task].target_url_name, rows[worker_task].target_pk) == (
        "workers:detail",
        contract.worker_id,
    )
    assert (rows[host_task].target_url_name, rows[host_task].target_pk) == (
        "hosts:detail",
        contract.host_organization_id,
    )
    assert rows[org_task].target_url_name == "" and rows[org_task].target_pk is None


def test_update_details_and_manual_target(contract: EmploymentContract) -> None:
    filing = task(contract)
    staff = make_staff(name="鈴木")
    assert filings.update_details(filing, by=None, assignee=staff, note="電話で確認")
    filing.refresh_from_db()
    assert filing.assignee == staff and filing.note == "電話で確認"
    assert not filings.update_details(filing, by=None, assignee=staff, note="電話で確認")
    assert filings.manual_target(worker=None, contract=contract, host=None) == contract
    with pytest.raises(Exception):  # noqa: B017, PT011
        filings.manual_target(worker=contract.worker, contract=contract, host=None)
    workers.deactivate(contract.worker, by=None, reason="重複")
    with pytest.raises(Exception):  # noqa: B017, PT011
        filings.manual_target(worker=contract.worker, contract=None, host=None)


# --- 先送りしていた 2 件（T09・T13b）: ダミーデータで拾えること --------------------------


@pytest.fixture
def dummy(settings: Settings, tmp_path: Path) -> None:
    settings.DEBUG = True
    settings.ALLOW_DUMMY_DATA = True
    settings.STORAGES = {
        **settings.STORAGES,
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path / "media")},
        },
    }
    call_command("seed_dummy", "--date", today().isoformat(), stdout=StringIO())


@pytest.mark.usefixtures("dummy")
def test_dummy_data_shows_unregistered_expiry_and_recently_left_workers() -> None:
    rows = dashboard.dashboard(DashboardQuery(), today()).rows
    kinds = {row.filing.kind for row in rows}
    # 期間満了なのに終了が未登録の契約 → 雇用契約の期間満了確認
    assert FilingKind.CONTRACT_EXPIRY_CHECK in kinds
    # 離職直後で届出が未提出の人 → 外国人一覧の既定からは消えるが、終了届出として出る
    left = [r for r in rows if r.filing.kind == FilingKind.CONTRACT_TERMINATION]
    assert left and all(r.filing.worker.status == WorkerStatus.LEFT_JOB for r in left)  # type: ignore[union-attr]
    levels = {row.level for row in rows}
    assert {deadlines.Level.OVERDUE, deadlines.Level.WITHIN_7, deadlines.Level.WITHIN_14} <= levels


# --- 画面 -----------------------------------------------------------------------


def test_default_assignee_is_mine_for_staff_and_all_for_admin(
    contract: EmploymentContract, support_staff_user: User, admin_user: User
) -> None:
    me = make_staff(name="自分", user=support_staff_user)
    mine = task(contract, assignee=me)
    task(contract, FilingKind.CONTRACT_TERMINATION)
    staff_rows = verified_client(support_staff_user).get("/").context["rows"]
    assert [row.filing for row in staff_rows] == [mine]
    admin_rows = verified_client(admin_user).get("/").context["rows"]
    assert len(admin_rows) == 2
    all_rows = verified_client(support_staff_user).get("/?assignee=all").context["rows"]
    assert len(all_rows) == 2


def test_dashboard_shows_counts_and_awaiting_chip(
    admin_client: Client, contract: EmploymentContract
) -> None:
    task(contract, days_ago=20)
    submitted = task(contract, FilingKind.CONTRACT_NEW)
    filings.transition(submitted, FilingStatus.SUBMITTED, by=None, submitted_on=today())
    html = admin_client.get("/").content.decode()
    # 期限のタブ（U2-D5）: すべて・期限超過・7日以内・14日以内・受理待ちと件数
    tabs = {tab.key: tab for tab in admin_client.get("/").context["level_tabs"]}
    assert list(tabs) == ["all", "overdue", "within_7", "within_14", "awaiting"]
    assert tabs["overdue"].count == 1 and tabs["overdue"].danger
    assert tabs["awaiting"].count == 1 and tabs["all"].selected
    assert 'data-level-count="overdue"' in html
    page = admin_client.get("/?status=submitted")
    assert [row.filing for row in page.context["rows"]] == [submitted]


def test_transition_from_processing_page(
    admin_client: Client, contract: EmploymentContract
) -> None:
    filing = task(contract)
    page = admin_client.get(f"/filings/{filing.pk}/")
    assert page.status_code == 200
    assert AuditLog.objects.filter(action=AuditAction.VIEW, object_id=str(filing.pk)).exists()
    # 提出日なしで「提出済」にはできない（T11 のルール）
    response = admin_client.post(
        f"/filings/{filing.pk}/",
        {"once_token": page.context["once_token"], "form": "transition", "to": "submitted"},
    )
    assert response.status_code == 200
    assert 'id="id_submitted_on-error"' in response.content.decode()
    response = admin_client.post(
        f"/filings/{filing.pk}/",
        {"once_token": response.context["once_token"], "form": "transition", "to": "in_progress"},
    )
    assert response.status_code == 302
    filing.refresh_from_db()
    assert filing.status == FilingStatus.IN_PROGRESS
    assert AuditLog.objects.filter(
        action=AuditAction.FILING_TRANSITION, object_id=str(filing.pk)
    ).exists()


def test_details_and_rebase_from_processing_page(
    admin_client: Client, contract: EmploymentContract
) -> None:
    filing = task(contract)
    staff = make_staff(name="鈴木")
    token = admin_client.get(f"/filings/{filing.pk}/").context["once_token"]
    admin_client.post(
        f"/filings/{filing.pk}/",
        {"once_token": token, "form": "details", "assignee": staff.pk, "note": "確認中"},
    )
    filing.refresh_from_db()
    assert filing.assignee == staff and filing.note == "確認中"
    token = admin_client.get(f"/filings/{filing.pk}/").context["once_token"]
    new_basis = today() - timedelta(days=3)
    admin_client.post(
        f"/filings/{filing.pk}/",
        {
            "once_token": token,
            "form": "rebase",
            "basis_on": new_basis.isoformat(),
            "reason": "事由の日の誤り",
        },
    )
    filing.refresh_from_db()
    assert filing.basis_on == new_basis


def test_processing_requires_manage(contract: EmploymentContract) -> None:
    filing = task(contract)
    viewer = make_user("viewer", roles=("support_staff",))
    viewer.user_roles.get().role.permissions.filter(action="filings.manage").delete()
    client = verified_client(viewer)
    page = client.get(f"/filings/{filing.pk}/")
    assert page.status_code == 200 and 'name="form"' not in page.content.decode()
    response = client.post(f"/filings/{filing.pk}/", {"form": "transition", "to": "in_progress"})
    assert response.status_code == 403
    assert client.get("/filings/new/").status_code == 403


def test_deactivated_workers_tasks_are_not_opened(
    admin_client: Client, contract: EmploymentContract
) -> None:
    filing = task(contract, FilingKind.CONTRACT_NEW)
    filings.transition(filing, FilingStatus.SUBMITTED, by=None, submitted_on=today())
    workers.deactivate(contract.worker, by=None, reason="重複")
    assert admin_client.get(f"/filings/{filing.pk}/").status_code == 404


def test_manual_filing_with_worker_prefilled(admin_client: Client) -> None:
    worker: ForeignWorker = make_worker()
    page = admin_client.get(f"/filings/new/?worker={worker.pk}")
    assert page.context["form"].initial["worker"] == worker
    response = admin_client.post(
        "/filings/new/",
        {
            "once_token": page.context["once_token"],
            "kind": FilingKind.MISSING,
            "basis_on": today().isoformat(),
            "worker": worker.pk,
        },
    )
    filing = Filing.objects.get(kind=FilingKind.MISSING)
    assert response["Location"] == f"/filings/{filing.pk}/"
    assert filing.worker == worker and filing.trigger == Trigger.MANUAL


def test_manual_filing_needs_exactly_one_target(
    admin_client: Client, contract: EmploymentContract
) -> None:
    page = admin_client.get("/filings/new/")
    response = admin_client.post(
        "/filings/new/",
        {
            "once_token": page.context["once_token"],
            "kind": FilingKind.MISSING,
            "basis_on": today().isoformat(),
            "worker": contract.worker_id,
            "host": contract.host_organization_id,
        },
    )
    assert response.status_code == 200
    assert "対象を 1 つだけ選んでください。" in response.content.decode()
    assert not Filing.objects.exists()
