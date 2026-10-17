"""相談・苦情対応（T20）: 受付・経過の追記と状態・通報とハローワーク相談・取消・集計・画面。"""

from datetime import date
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.test import Client

from apps.core.models import AuditAction, AuditLog
from apps.support.models import (
    Consultation,
    ConsultationEntry,
    ConsultationStatus,
    ImmutableRowError,
    SupportRecord,
)
from services import consultations, support_records, workers
from services.consultations import EntryInput, ReceptionInput
from services.support_catalog import SupportItem
from services.support_records import RecordInput
from tests.contracts.factories import make_contract, site_with_categories
from tests.core.factories import make_staff
from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 17)


@pytest.fixture
def worker() -> Any:
    worker = make_worker(assigned_staff=make_staff(name="佐藤"))
    make_contract(worker, site=site_with_categories())
    return worker


def reception(worker: Any, **overrides: Any) -> ReceptionInput:
    record_overrides = overrides.pop("record", {})
    record = RecordInput(
        **(
            {
                "kind": SupportItem.CONSULTATION,
                "occurred_on": date(2026, 10, 1),
                "method": "message",
                "staff": worker.assigned_staff,
                "body": "残業代が少ないという相談。",
            }
            | record_overrides
        )
    )
    values: dict[str, Any] = {"record": record, "category": "wages"}
    return ReceptionInput(**(values | overrides))


def entry(worker: Any, **overrides: Any) -> EntryInput:
    values: dict[str, Any] = {
        "occurred_on": date(2026, 10, 5),
        "method": "phone",
        "staff": worker.assigned_staff,
        "body": "受入れ機関に給与明細を確認した。",
        "status_after": ConsultationStatus.IN_PROGRESS,
    }
    return EntryInput(**(values | overrides))


def receive(worker: Any, **overrides: Any) -> Consultation:
    return consultations.create_consultation(
        worker, reception(worker, **overrides), by=None, today=TODAY
    )


def keys(error: pytest.ExceptionInfo[ValidationError]) -> set[str]:
    return set(error.value.error_dict)


# --- 受付 -------------------------------------------------------------------------------


def test_reception_creates_a_record_and_a_case(worker: Any) -> None:
    case = receive(worker, record={"counterpart": "supervisor", "counterpart_name": "上司"})
    record = case.record
    assert (record.kind, record.method, record.counterpart, record.counterpart_name) == (
        "consultation",
        "message",
        "worker",  # 相手は本人に固定
        "",
    )
    assert (case.category, case.status, case.initial_status) == ("wages", "received", "received")
    assert AuditLog.objects.filter(
        action=AuditAction.CREATE, content_type__model="consultation", object_id=str(case.pk)
    ).exists()


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"category": ""}, {"category"}),
        ({"category": "other"}, {"category_note"}),
        ({"status": "handed_over"}, {"handed_over_to"}),
        ({"handed_over_to": "労基署"}, {"handed_over_to"}),
        ({"record": {"body": ""}}, {"body"}),
    ],
)
def test_reception_validation(worker: Any, overrides: dict[str, Any], expected: set[str]) -> None:
    with pytest.raises(ValidationError) as error:
        consultations.validate_reception(worker, reception(worker, **overrides), today=TODAY)
    assert keys(error) == expected


def test_message_method_is_only_for_consultations(worker: Any) -> None:
    data = RecordInput(
        kind=SupportItem.EXCHANGE,
        occurred_on=date(2026, 10, 1),
        method="message",
        staff=worker.assigned_staff,
        body="交流",
        details={"event": "夏祭り"},
    )
    with pytest.raises(ValidationError) as error:
        support_records.validate(worker, data, today=TODAY)
    assert keys(error) == {"method"}


# --- 経過 -------------------------------------------------------------------------------


def test_entries_change_status_and_keep_the_timeline(worker: Any) -> None:
    case = receive(worker)
    consultations.add_entry(case, entry(worker), by=None, today=TODAY)
    consultations.add_entry(
        case,
        entry(
            worker,
            occurred_on=date(2026, 10, 3),  # 前の日付の経過を後から入れても日付順に並ぶ
            body="本人から追加の聞き取り。",
            status_after=ConsultationStatus.IN_PROGRESS,
        ),
        by=None,
        today=TODAY,
    )
    consultations.add_entry(
        case,
        entry(
            worker,
            occurred_on=date(2026, 10, 9),
            body="差額が支払われた。",
            status_after="resolved",
        ),
        by=None,
        today=TODAY,
    )
    case.refresh_from_db()
    assert case.status == ConsultationStatus.RESOLVED
    items = consultations.timeline(case)
    assert [(i.kind, i.occurred_on, i.status) for i in items] == [
        ("reception", date(2026, 10, 1), "received"),
        ("entry", date(2026, 10, 3), "in_progress"),
        ("entry", date(2026, 10, 5), "in_progress"),
        ("entry", date(2026, 10, 9), "resolved"),
    ]
    # 解決からも再開できる
    consultations.add_entry(
        case, entry(worker, occurred_on=date(2026, 10, 12)), by=None, today=TODAY
    )
    case.refresh_from_db()
    assert case.status == ConsultationStatus.IN_PROGRESS


def test_entries_are_append_only(worker: Any) -> None:
    case = receive(worker)
    added = consultations.add_entry(case, entry(worker), by=None, today=TODAY)
    added.body = "書き換え"
    with pytest.raises(ImmutableRowError):
        added.save()
    with pytest.raises(ImmutableRowError):
        added.delete()


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"status_after": "handed_over"}, {"handed_over_to"}),
        ({"reported": True}, {"report_to"}),
        ({"reported": True, "report_to": "other"}, {"report_to_note"}),
        ({"report_to": "immigration"}, {"reported"}),
        ({"hello_work_on": date(2026, 10, 5)}, {"hello_work"}),
        ({"hello_work": True, "hello_work_on": date(2026, 10, 18)}, {"hello_work_on"}),
        ({"occurred_on": date(2026, 9, 30)}, {"occurred_on"}),  # 受付より前
        ({"method": "fax"}, {"method"}),
    ],
)
def test_entry_validation(worker: Any, overrides: dict[str, Any], expected: set[str]) -> None:
    case = receive(worker)
    with pytest.raises(ValidationError) as error:
        consultations.validate_entry(case, entry(worker, **overrides), today=TODAY)
    assert keys(error) == expected


def test_report_and_hello_work_flags(worker: Any) -> None:
    case = receive(worker, category="employment")
    consultations.add_entry(
        case,
        entry(
            worker,
            status_after="handed_over",
            handed_over_to="○○労働基準監督署",
            reported=True,
            report_to="labor_standards_office",
            reported_on=date(2026, 10, 5),
        ),
        by=None,
        today=TODAY,
    )
    consultations.add_entry(
        case,
        entry(worker, occurred_on=date(2026, 10, 7), hello_work=True),  # 相談日は任意
        by=None,
        today=TODAY,
    )
    case.refresh_from_db()
    assert (case.reported, case.hello_work, case.status, case.handed_over_to) == (
        True,
        True,
        "in_progress",
        "",  # 引継ぎから戻したので引継ぎ先は空
    )
    rows = ConsultationEntry.objects.filter(consultation=case).order_by("pk")
    assert [(r.reported_on, r.hello_work_on) for r in rows] == [(date(2026, 10, 5), None)] + [
        (None, None)
    ]


# --- 取消 -------------------------------------------------------------------------------


def test_void_the_whole_case_only(worker: Any) -> None:
    case = receive(worker)
    consultations.add_entry(case, entry(worker), by=None, today=TODAY)
    with pytest.raises(ValidationError):
        support_records.void_record(case.record, by=None, reason="誤り")
    with pytest.raises(ValidationError):
        consultations.void_consultation(case, by=None, reason=" ")
    consultations.void_consultation(case, by=None, reason="別の人の相談")
    case.refresh_from_db()
    assert case.voided_at is not None
    assert SupportRecord.objects.get(pk=case.record_id).voided_at is not None
    assert case.entries.count() == 1  # 経過は残る
    with pytest.raises(ValidationError) as error:
        consultations.validate_entry(case, entry(worker), today=TODAY)
    assert "__all__" in keys(error)


# --- 集計 -------------------------------------------------------------------------------


def test_summary_counts_match_by_category(worker: Any) -> None:
    receive(worker, category="wages")
    receive(worker, category="wages", record={"occurred_on": date(2026, 9, 20)})
    flagged = receive(worker, category="health")
    consultations.add_entry(flagged, entry(worker, hello_work=True), by=None, today=TODAY)
    voided = receive(worker, category="daily_life")
    consultations.void_consultation(voided, by=None, reason="重複")
    receive(worker, category="daily_life", record={"occurred_on": date(2026, 8, 31)})  # 期間外
    result = consultations.summary([worker.pk], date(2026, 9, 1), date(2026, 10, 31))
    item = result[worker.pk]
    assert item.total == 3 == sum(item.by_category.values())
    assert item.by_category == {"wages": 2, "health": 1}
    assert (item.hello_work, item.reported, item.open) == (1, 0, 3)


# --- 画面 -------------------------------------------------------------------------------


def test_receive_and_add_entries_on_screens(support_staff_client: Client, worker: Any) -> None:
    url = f"/workers/{worker.pk}/support/consultations/new/"
    page = support_staff_client.get(url)
    html = page.content.decode()
    assert 'value="message"' in html and 'name="counterpart"' not in html
    assert "複数にまたがる場合は主なもの 1 つ" in html and "data-keepalive=" in html
    response = support_staff_client.post(
        url,
        {
            "once_token": page.context["once_token"],
            "occurred_on": "2026-10-01",
            "method": "phone",
            "staff": worker.assigned_staff_id,
            "category": "relationships",
            "status": "received",
            "body_language": "zh-Hans",
            "body": "同僚との関係で悩んでいる。",
        },
    )
    case = Consultation.objects.get(worker=worker)
    detail_url = f"/workers/{worker.pk}/support/consultations/{case.pk}/"
    assert response["Location"] == detail_url

    entry_url = f"{detail_url}entries/new/"
    page = support_staff_client.get(entry_url)
    form = page.context["form"]
    assert form["status_after"].value() == "received"
    bad = support_staff_client.post(
        entry_url,
        {
            "once_token": page.context["once_token"],
            "occurred_on": "2026-10-02",
            "method": "in_person",
            "staff": worker.assigned_staff_id,
            "status_after": "handed_over",
            "body_language": "ja",
            "body": "引き継ぐ。",
        },
    )
    assert 'id="id_handed_over_to-error"' in bad.content.decode()
    page = support_staff_client.get(entry_url)
    support_staff_client.post(
        entry_url,
        {
            "once_token": page.context["once_token"],
            "occurred_on": "2026-10-02",
            "method": "in_person",
            "staff": worker.assigned_staff_id,
            "status_after": "resolved",
            "body_language": "ja",
            "body": "配置換えで解決。",
            "hello_work": "on",
        },
    )
    case.refresh_from_db()
    assert (case.status, case.hello_work) == ("resolved", True)
    shown = support_staff_client.get(detail_url)
    html = shown.content.decode()
    assert html.index("同僚との関係で悩んでいる。") < html.index("配置換えで解決。")
    assert 'data-flag="hello_work"' in html
    log = AuditLog.objects.filter(action=AuditAction.VIEW, object_id=str(worker.pk)).latest("pk")
    assert log.context["shown"]["consultation"] == case.pk


def test_list_defaults_to_unresolved_and_is_audited(
    support_staff_client: Client, worker: Any
) -> None:
    open_case = receive(worker)
    done = receive(worker, category="health")
    consultations.add_entry(done, entry(worker, status_after="resolved"), by=None, today=TODAY)
    page = support_staff_client.get("/consultations/")
    assert [item.pk for item in page.context["page"].object_list] == [open_case.pk]
    assert 'href="/consultations/"' in page.content.decode()  # ナビの「相談」
    log = AuditLog.objects.filter(action=AuditAction.VIEW).latest("pk")
    assert log.context["screen"] == "consultations:list"
    resolved = support_staff_client.get("/consultations/", {"status": "resolved"})
    assert [item.pk for item in resolved.context["page"].object_list] == [done.pk]
    everything = support_staff_client.get(
        "/consultations/", {"status": "all", "category": "health"}
    )
    assert [item.pk for item in everything.context["page"].object_list] == [done.pk]


def test_records_list_links_to_the_case(support_staff_client: Client, worker: Any) -> None:
    case = receive(worker)
    html = support_staff_client.get(f"/workers/{worker.pk}/support/records/").content.decode()
    assert f"/support/consultations/{case.pk}/" in html
    void_one = f"/workers/{worker.pk}/support/records/{case.record_id}/void/"
    assert support_staff_client.get(void_one).status_code == 404


def test_void_screen_and_deactivated_read_only(admin_client: Client, worker: Any) -> None:
    case = receive(worker)
    url = f"/workers/{worker.pk}/support/consultations/{case.pk}/void/"
    page = admin_client.get(url)
    response = admin_client.post(
        url, {"once_token": page.context["once_token"], "reason": "別の人の相談"}
    )
    assert response.status_code == 302
    case.refresh_from_db()
    assert case.voided_at is not None
    other = receive(worker)
    workers.deactivate(worker, by=None, reason="重複")
    detail = admin_client.get(f"/workers/{worker.pk}/support/consultations/{other.pk}/")
    assert detail.status_code == 200 and "/entries/new/" not in detail.content.decode()
    entry_url = f"/workers/{worker.pk}/support/consultations/{other.pk}/entries/new/"
    assert admin_client.get(entry_url).status_code == 403
