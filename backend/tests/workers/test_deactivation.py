"""外国人の無効化（T13e）: 取消・届出の取消・監査ログ・対象から外す・画面の権限・admin で戻す。"""

from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.utils import timezone

from apps.core.models import AuditAction, AuditLog, User
from apps.filings.models import Filing, FilingKind, FilingStatus, Trigger
from apps.workers.models import ForeignWorker
from services import filings, workers
from services.worker_list import WorkerListQuery, list_workers
from tests.contracts.factories import make_contract
from tests.core.factories import make_user, verified_client
from tests.workers.factories import add_passport, add_residence, make_worker

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 16)


@pytest.fixture
def worker() -> ForeignWorker:
    worker = make_worker(status="residing")
    add_residence(worker, card_number="AB12345678CD", expires_on=date(2026, 12, 1))
    add_passport(worker)
    return worker


def open_filing(worker: ForeignWorker) -> Filing:
    contract = make_contract(worker)
    filing, _created = filings.create(
        FilingKind.CONTRACT_CHANGE, target=contract, basis_on=TODAY, trigger=Trigger.MANUAL
    )
    return filing


def test_deactivate_voids_records_and_cancels_open_filings(worker: ForeignWorker) -> None:
    admin = make_user("kanri", roles=("admin",))
    filing = open_filing(worker)
    effects = workers.deactivate(worker, by=admin, reason="重複して登録した")
    worker.refresh_from_db()
    assert worker.deactivated_by == admin and worker.deactivation_reason == "重複して登録した"
    assert len(effects.residence_records) == 1 and len(effects.passport_records) == 1
    assert not worker.residence_records.filter(voided_at__isnull=True).exists()
    assert not worker.passport_records.filter(voided_at__isnull=True).exists()
    assert worker.residence_records.get().void_reason == "外国人の無効化"
    filing.refresh_from_db()
    assert filing.status == FilingStatus.CANCELLED and filing.status_reason == "外国人の無効化"


def test_deactivate_records_one_audit_event_with_changes(worker: ForeignWorker) -> None:
    filing = open_filing(worker)
    before = AuditLog.objects.count()
    workers.deactivate(worker, by=None, reason="重複して登録した")
    logs = AuditLog.objects.order_by("pk")[before:]
    assert [log.action for log in logs] == [AuditAction.WORKER_DEACTIVATE]
    entry = logs[0]
    assert entry.changes["deactivation_reason"] == ["", "重複して登録した"]
    assert entry.changes["cancelled_filings"] == [
        [filing.pk, FilingKind.CONTRACT_CHANGE, FilingStatus.NOT_STARTED]
    ]
    assert len(entry.changes["voided_residence_records"]) == 1


def test_deactivate_requires_reason_and_active_worker(worker: ForeignWorker) -> None:
    with pytest.raises(ValidationError):
        workers.deactivate(worker, by=None, reason=" ")
    workers.deactivate(worker, by=None, reason="理由")
    with pytest.raises(ValidationError):
        workers.deactivate(worker, by=None, reason="もう一度")


def test_deactivated_workers_are_excluded_from_list_and_daily_filings(
    worker: ForeignWorker,
) -> None:
    user = make_user("tanto", roles=("support_staff",))
    workers.deactivate(worker, by=None, reason="理由")
    assert list_workers(user, WorkerListQuery(statuses=()), TODAY).rows == []
    included = list_workers(user, WorkerListQuery(statuses=(), include_deactivated=True), TODAY)
    assert [row.worker for row in included.rows] == [worker]
    assert worker not in workers.active_workers()

    # 在留期限が 3 か月以内でも起票しない。番号での検索にも出ない
    add_residence(worker, card_number="ZZ11111111YY", expires_on=date(2026, 12, 1))
    filings.create_scheduled(TODAY)
    assert not Filing.objects.filter(worker=worker, kind=FilingKind.RESIDENCE_RENEWAL).exists()
    assert list_workers(user, WorkerListQuery(text="ZZ11111111YY"), TODAY).rows == []


def test_event_driven_filings_are_not_created_for_deactivated_workers(
    worker: ForeignWorker,
) -> None:
    contract = make_contract(worker)
    workers.deactivate(worker, by=None, reason="理由")
    filing, created = filings.create_if_active(
        FilingKind.CONTRACT_CHANGE, target=contract, basis_on=TODAY, trigger=Trigger.MANUAL
    )
    assert filing is None and not created


def test_reactivate_keeps_both_reasons_in_audit(worker: ForeignWorker) -> None:
    workers.deactivate(worker, by=None, reason="重複して登録した")
    with pytest.raises(ValidationError):
        workers.reactivate(worker, by=None, reason="")
    workers.reactivate(worker, by=None, reason="別人だった")
    worker.refresh_from_db()
    assert worker.deactivated_at is None
    entry = AuditLog.objects.filter(action=AuditAction.WORKER_REACTIVATE).get()
    assert entry.context == {
        "deactivation_reason": "重複して登録した",
        "reactivation_reason": "別人だった",
    }
    # 取り消した行は戻さない
    assert not worker.residence_records.filter(voided_at__isnull=True).exists()


# --- 画面 -----------------------------------------------------------------------


def test_support_staff_cannot_deactivate(
    support_staff_client: Client, worker: ForeignWorker
) -> None:
    assert support_staff_client.get(f"/workers/{worker.pk}/deactivate/").status_code == 403
    html = support_staff_client.get(f"/workers/{worker.pk}/").content.decode()
    assert "/deactivate/" not in html


def test_admin_deactivates_through_confirmation(
    admin_client: Client, worker: ForeignWorker
) -> None:
    open_filing(worker)
    url = f"/workers/{worker.pk}/deactivate/"
    page = admin_client.get(url)
    html = page.content.decode()
    assert "雇用契約変更届出" in html  # 取消になる届出を先に見せる
    response = admin_client.post(url, {"once_token": page.context["once_token"], "reason": ""})
    assert 'id="id_reason-error"' in response.content.decode()
    response = admin_client.post(
        url, {"once_token": response.context["once_token"], "reason": "重複して登録した"}
    )
    assert response["Location"] == f"/workers/{worker.pk}/"
    detail = admin_client.get(f"/workers/{worker.pk}/").content.decode()
    assert 'id="deactivated-banner"' in detail and "重複して登録した" in detail
    assert f"/workers/{worker.pk}/edit/" not in detail  # 無効化済みは編集させない
    assert admin_client.get(f"/workers/{worker.pk}/edit/").status_code == 403


def test_include_deactivated_filter_is_admin_only(
    admin_client: Client, support_staff_client: Client, worker: ForeignWorker
) -> None:
    workers.deactivate(worker, by=None, reason="理由")
    assert 'name="include_deactivated"' in admin_client.get("/workers/").content.decode()
    assert (
        'name="include_deactivated"' not in support_staff_client.get("/workers/").content.decode()
    )
    # 支援担当者が URL で指定しても無視される
    rows = support_staff_client.get("/workers/?include_deactivated=on&status=all").context["rows"]
    assert rows == []
    rows = admin_client.get("/workers/?include_deactivated=on&status=all").context["rows"]
    assert [row.worker for row in rows] == [worker]


def test_admin_reactivation_page_requires_reason(admin_user: User, worker: ForeignWorker) -> None:
    workers.deactivate(worker, by=None, reason="重複して登録した")
    client = verified_client(admin_user)
    url = f"/admin/workers/foreignworker/{worker.pk}/reactivate/"
    page = client.get(url)
    assert page.status_code == 200 and "重複して登録した" in page.content.decode()
    assert client.post(url, {"reason": ""}).status_code == 200
    response = client.post(url, {"reason": "別人だった"})
    assert response.status_code == 302
    worker.refresh_from_db()
    assert worker.deactivated_at is None
    assert worker.updated_at <= timezone.now()
