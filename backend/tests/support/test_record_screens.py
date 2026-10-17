"""支援実績の画面（T18）: 「支援」タブの「記録」の一覧・入力・詳細・取消・添付の追加と取消。"""

from datetime import date
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone

from apps.core.models import AuditAction, AuditLog, SupportLanguage
from apps.support.models import SupportRecord
from services import support_records, workers
from services.support_catalog import SupportItem
from services.support_records import RecordInput
from tests.conftest import PDF_BYTES
from tests.contracts.factories import make_contract, site_with_categories
from tests.core.factories import make_staff, make_user, verified_client
from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db


@pytest.fixture
def worker() -> Any:
    staff = make_staff(name="佐藤", position="支援担当")
    worker = make_worker(assigned_staff=staff)
    make_contract(worker, site=site_with_categories())
    return worker


def records_url(worker: Any) -> str:
    return f"/workers/{worker.pk}/support/records/"


def make_record(worker: Any, **overrides: Any) -> SupportRecord:
    values: dict[str, Any] = {
        "kind": SupportItem.EXCHANGE,
        "occurred_on": date(2026, 9, 1),
        "method": "in_person",
        "staff": worker.assigned_staff,
        "body": "地域の夏祭りに同行した。",
        "details": {"event": "夏祭り"},
    }
    return support_records.create_record(worker, RecordInput(**(values | overrides)), by=None)


def post_transfer(client: Client, worker: Any, **overrides: Any) -> Any:
    url = f"/workers/{worker.pk}/support/records/new/airport_transfer/"
    page = client.get(url)
    assert page.status_code == 200
    data: dict[str, Any] = {
        "once_token": page.context["once_token"],
        "occurred_on": timezone.localdate().isoformat(),
        "method": "in_person",
        "staff": worker.assigned_staff_id,
        "counterpart": "worker",
        "languages": [SupportLanguage.objects.get(code="zh-hans").pk],
        "body_language": "zh-Hans",
        "body": "在成田机场迎接，送到宿舍。",
        "detail_direction": "2.a",
        "detail_airport": "成田国際空港",
        "detail_transport": "社用車",
    }
    data.update(overrides)
    return client.post(url, data)


def test_create_form_has_defaults_and_keepalive(support_staff_client: Client, worker: Any) -> None:
    page = support_staff_client.get(f"/workers/{worker.pk}/support/records/new/airport_transfer/")
    form = page.context["form"]
    assert form["staff"].value() == worker.assigned_staff_id
    assert form["body_language"].value() == "zh-Hans"
    assert form["languages"].value() == [worker.support_language_id]
    html = page.content.decode()
    assert "data-keepalive=" in html and 'enctype="multipart/form-data"' in html
    assert 'name="detail_airport"' in html


def test_unknown_kind_is_404(support_staff_client: Client, worker: Any) -> None:
    url = f"/workers/{worker.pk}/support/records/new/periodic_interview/"
    assert support_staff_client.get(url).status_code == 404


def test_create_record_with_attachments(support_staff_client: Client, worker: Any) -> None:
    files = [
        SimpleUploadedFile("確認書.pdf", PDF_BYTES, content_type="application/pdf"),
        SimpleUploadedFile("写真.pdf", PDF_BYTES, content_type="application/pdf"),
    ]
    response = post_transfer(support_staff_client, worker, uploads=files)
    record = SupportRecord.objects.get(worker=worker)
    assert response["Location"] == f"/workers/{worker.pk}/support/records/{record.pk}/"
    assert record.details["airport"] == "成田国際空港"
    assert record.attachments.count() == 2
    detail = support_staff_client.get(response["Location"])
    html = detail.content.decode()
    assert 'lang="zh-Hans"' in html and "確認書.pdf" in html
    entry = AuditLog.objects.filter(action=AuditAction.VIEW, object_id=str(worker.pk)).latest("pk")
    assert entry.context["shown"]["support_record"] == record.pk


def test_service_errors_are_shown_under_fields(support_staff_client: Client, worker: Any) -> None:
    response = post_transfer(support_staff_client, worker, detail_airport="", body="")
    assert response.status_code == 200
    html = response.content.decode()
    assert 'id="id_detail_airport-error"' in html and 'id="id_body-error"' in html
    assert not SupportRecord.objects.exists()


def test_double_submit_creates_one_record(support_staff_client: Client, worker: Any) -> None:
    url = f"/workers/{worker.pk}/support/records/new/airport_transfer/"
    page = support_staff_client.get(url)
    data = {
        "once_token": page.context["once_token"],
        "occurred_on": "2026-04-01",
        "method": "in_person",
        "staff": worker.assigned_staff_id,
        "counterpart": "worker",
        "body_language": "ja",
        "body": "送迎した。",
        "detail_direction": "2.a",
        "detail_airport": "成田国際空港",
    }
    support_staff_client.post(url, data)
    support_staff_client.post(url, data)
    assert SupportRecord.objects.count() == 1


def test_list_filters_and_void_strikethrough(support_staff_client: Client, worker: Any) -> None:
    other_staff = make_staff(name="鈴木")
    first = make_record(worker)
    second = make_record(
        worker,
        kind=SupportItem.PUBLIC_PROCEDURES,
        occurred_on=date(2026, 10, 1),
        staff=other_staff,
        details={"procedure": "転入届"},
    )
    page = support_staff_client.get(records_url(worker))
    assert [record.pk for record, _details in page.context["records"]] == [second.pk, first.pk]
    assert "計画なし" in page.content.decode()
    filtered = support_staff_client.get(records_url(worker), {"kind": "exchange"})
    assert [record.pk for record, _details in filtered.context["records"]] == [first.pk]
    by_staff = support_staff_client.get(records_url(worker), {"staff": other_staff.pk})
    assert [record.pk for record, _details in by_staff.context["records"]] == [second.pk]
    period = support_staff_client.get(
        records_url(worker), {"date_from": "2026-09-15", "date_to": "2026-10-31"}
    )
    assert [record.pk for record, _details in period.context["records"]] == [second.pk]
    entry = AuditLog.objects.filter(action=AuditAction.VIEW, object_id=str(worker.pk)).latest("pk")
    assert entry.context["shown"]["support_records"] == [second.pk]

    url = f"/workers/{worker.pk}/support/records/{first.pk}/void/"
    confirm = support_staff_client.get(url)
    assert "夏祭り" in confirm.content.decode()
    empty = support_staff_client.post(url, {"once_token": confirm.context["once_token"]})
    assert empty.status_code == 200 and 'id="id_reason-error"' in empty.content.decode()
    again = support_staff_client.get(url)
    response = support_staff_client.post(
        url, {"once_token": again.context["once_token"], "reason": "別の人の記録"}
    )
    assert response["Location"] == records_url(worker)
    html = support_staff_client.get(records_url(worker)).content.decode()
    assert f'data-record="{first.pk}" class="is-voided"' in html and "別の人の記録" in html
    assert support_staff_client.get(url).status_code == 404  # 取り消した記録は再び取り消せない


def test_add_and_void_attachment(support_staff_client: Client, worker: Any) -> None:
    record = make_record(worker)
    url = f"/workers/{worker.pk}/support/records/{record.pk}/attachments/new/"
    page = support_staff_client.get(url)
    bad = support_staff_client.post(
        url,
        {
            "once_token": page.context["once_token"],
            "uploads": [SimpleUploadedFile("memo.txt", b"x", content_type="text/plain")],
        },
    )
    assert bad.status_code == 200 and 'id="id_uploads-error"' in bad.content.decode()
    page = support_staff_client.get(url)
    support_staff_client.post(
        url,
        {
            "once_token": page.context["once_token"],
            "uploads": [
                SimpleUploadedFile("確認書.pdf", PDF_BYTES, content_type="application/pdf")
            ],
        },
    )
    attachment = record.attachments.get()
    void_url = f"/workers/{worker.pk}/support/records/{record.pk}/attachments/{attachment.pk}/void/"
    confirm = support_staff_client.get(void_url)
    response = support_staff_client.post(
        void_url, {"once_token": confirm.context["once_token"], "reason": "別の人の書類"}
    )
    assert response["Location"] == f"/workers/{worker.pk}/support/records/{record.pk}/"
    attachment.refresh_from_db()
    assert attachment.voided_at is not None and attachment.file.read() == PDF_BYTES
    html = support_staff_client.get(response["Location"]).content.decode()
    assert f'data-attachment="{attachment.pk}" class="is-voided"' in html
    assert "別の人の書類" in html
    assert support_staff_client.get(void_url).status_code == 404
    download = support_staff_client.get(f"/attachments/{attachment.pk}/download/")
    assert download.status_code == 200  # 取り消しても、ファイルは残っていて見られる


def test_deactivated_worker_records_are_read_only(admin_client: Client, worker: Any) -> None:
    record = make_record(worker)
    workers.deactivate(worker, by=None, reason="重複")
    html = admin_client.get(records_url(worker)).content.decode()
    assert "/records/new/" not in html and "/void/" not in html
    assert admin_client.get(f"{records_url(worker)}{record.pk}/").status_code == 200
    url = f"/workers/{worker.pk}/support/records/new/exchange/"
    assert admin_client.get(url).status_code == 403
    assert admin_client.get(f"{records_url(worker)}{record.pk}/void/").status_code == 403


def test_viewer_without_manage_cannot_create(worker: Any) -> None:
    viewer = make_user("viewer", roles=("support_staff",))
    viewer.user_roles.get().role.permissions.filter(action="support.manage").delete()
    client = verified_client(viewer)
    html = client.get(records_url(worker)).content.decode()
    assert "/records/new/" not in html
    assert client.get(f"/workers/{worker.pk}/support/records/new/exchange/").status_code == 403
