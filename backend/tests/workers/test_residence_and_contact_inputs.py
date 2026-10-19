"""在留情報の任意の区画・入国前（修正 1 回目 B5）と、住所・連絡先の適用開始日（B6）。"""

import datetime

import pytest
from django.utils import timezone

from apps.workers.models import (
    ApplicationType,
    LatestWorkerContact,
    StatusOfResidence,
    WorkerContact,
    WorkerStatus,
)
from services import worker_detail, worker_list, workers
from services.worker_list import WorkerListQuery
from tests.core.factories import make_user, verified_client
from tests.workers.factories import add_residence, make_worker

pytestmark = pytest.mark.django_db


def coe_worker() -> object:
    worker = make_worker(status=WorkerStatus.PRE_ENTRY)
    add_residence(
        worker,
        card_number="",
        status_of_residence=StatusOfResidence.SSW_1,
        application_type=ApplicationType.CERTIFICATE_OF_ELIGIBILITY,
        permitted_on=timezone.localdate() - datetime.timedelta(days=10),
        expires_on=timezone.localdate() + datetime.timedelta(days=30),
    )
    return worker


def test_certificate_of_eligibility_is_not_a_period_of_stay() -> None:
    worker = coe_worker()
    admin = make_user("kanri", roles=("admin",))
    rows = worker_list.list_workers(admin, WorkerListQuery(statuses=()), timezone.localdate()).rows
    row = next(r for r in rows if r.worker.pk == worker.pk)  # type: ignore[attr-defined]
    assert row.deadline is None and row.level is None and row.expires_on is None
    info = worker_detail.basic_info(worker)  # type: ignore[arg-type]
    assert info.residence_deadline is None
    page = verified_client(admin).get(f"/workers/{worker.pk}/").content.decode()  # type: ignore[attr-defined]
    assert 'id="coe-valid-until"' in page


def test_register_screen_marks_optional_sections_and_the_pre_entry_note() -> None:
    client = verified_client(make_user("tanto", roles=("support_staff",)))
    page = client.get("/workers/new/").content.decode()
    assert "入れる場合は必須" in page and 'id="pre-entry-note"' in page
    worker = coe_worker()
    form = client.get(f"/workers/{worker.pk}/residence/new/").content.decode()  # type: ignore[attr-defined]
    assert 'id="pre-entry-note"' in form


def test_contact_without_a_date_counts_from_its_registration() -> None:
    worker = make_worker()
    dated = WorkerContact(worker=worker, phone="000-1", effective_from=datetime.date(2020, 1, 1))
    workers.add_record(worker, dated, by=None)
    undated = WorkerContact(worker=worker, phone="000-2", home_country_address="Sample City")
    workers.add_record(worker, undated, by=None)
    latest = LatestWorkerContact.objects.get(worker=worker)
    assert latest.record == undated  # 登録した日（今日）が 2020-01-01 より新しい
    assert undated.effective_on == timezone.localdate()
    assert list(worker.contacts.all()) == [undated, dated]
    client = verified_client(make_user("tanto", roles=("support_staff",)))
    page = client.get(f"/workers/{worker.pk}/").content.decode()
    assert "適用開始日なし" in page


def test_effective_date_comes_after_the_main_fields() -> None:
    client = verified_client(make_user("tanto", roles=("support_staff",)))
    worker = make_worker()
    for kind, first in (("address", "postal_code"), ("contact", "phone")):
        form = client.get(f"/workers/{worker.pk}/{kind}/new/").context["form"]
        names = list(form.fields)
        assert names[0] == first and names[-1] == "effective_from"
    contact_form = client.get(f"/workers/{worker.pk}/contact/new/").context["form"]
    address_form = client.get(f"/workers/{worker.pk}/address/new/").context["form"]
    assert contact_form.fields["effective_from"].required is False
    assert address_form.fields["effective_from"].required is True
