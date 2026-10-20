"""帳簿（T44、ADR-0036）: 足りない記録の欄・本人の申出で実施しなかった記録・当時の役職・
上司のオンライン面談の同意の添付・保存義務の満了日の目安。"""

from datetime import date
from typing import Any

import pytest
from django.core.files.base import ContentFile
from django.test import Client

from apps.contracts.models import EmploymentContract
from apps.core.models import Attachment, AttachmentKind
from apps.support.models import Consultation, RecordMethod, SupportRecord
from apps.workers.models import WorkerStatus
from services import books, consultations, periodic_report, support_interviews, support_records
from services.consultations import ReceptionInput
from services.support_catalog import SupportItem
from services.support_records import RecordInput
from tests.conftest import PDF_BYTES
from tests.contracts.factories import make_contract, site_with_categories
from tests.core.factories import make_staff
from tests.support.test_periodic_interviews import interview, row
from tests.support.test_periodic_report import full_support
from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 17)


@pytest.fixture
def worker() -> Any:
    staff = make_staff(name="佐藤", position="支援担当")
    worker = make_worker(assigned_staff=staff, status=WorkerStatus.RESIDING)
    make_contract(worker, site=site_with_categories())  # 2026-04-01〜2027-03-31
    return worker


def record(worker: Any, kind: SupportItem, details: dict[str, Any], **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "kind": kind,
        "occurred_on": date(2026, 6, 1),
        "method": RecordMethod.IN_PERSON,
        "staff": worker.assigned_staff,
        "body": "記録の本文。",
        "details": details,
    }
    return support_records.create_record(
        worker, RecordInput(**(values | overrides)), by=None, today=TODAY
    )


def test_optional_book_fields_are_kept(worker: Any) -> None:
    """帳簿④ の足りなかった欄（任意）: 実施場所・転職先候補企業・通訳人・講師・保証人契約。"""
    exchange = record(
        worker, SupportItem.EXCHANGE, {"event": "夏祭り", "place": "船橋市 中央公民館"}
    )
    assert exchange.details["place"] == "船橋市 中央公民館"
    job = record(
        worker,
        SupportItem.JOB_CHANGE,
        {
            "separation": "involuntary",
            "activities": ["8.a"],
            "place": "本社 会議室",
            "candidates": "株式会社サンプル（千葉県船橋市、047-000-0000）",
            "interpreter": "陳 美玲（通訳会社）",
        },
    )
    assert job.details["candidates"].startswith("株式会社サンプル")
    lesson = record(
        worker, SupportItem.JAPANESE_LEARNING, {"ways": ["5.c"], "instructor": "山田 講師"}
    )
    assert lesson.details["instructor"] == "山田 講師"
    housing = record(
        worker,
        SupportItem.HOUSING_AND_CONTRACTS,
        {"activities": ["3a.a"], "guarantor": "家賃保証会社と保証契約（受入れ機関が連帯保証）"},
    )
    assert "家賃保証会社" in housing.details["guarantor"]
    # どれも任意（無くても保存できる）
    assert "place" not in record(worker, SupportItem.EXCHANGE, {"event": "交流会"}).details


def test_staff_position_at_the_time_and_declined_records(worker: Any) -> None:
    exchange = record(worker, SupportItem.EXCHANGE, {"event": "交流会"})
    staff = worker.assigned_staff
    staff.position = "支援責任者"
    staff.save()
    exchange.refresh_from_db()
    assert exchange.staff_position == "支援担当"  # 保存のときの役職のまま（T44-D6）
    declined = record(
        worker,
        SupportItem.EXCHANGE,
        {"event": "地域の清掃活動"},
        occurred_on=date(2026, 7, 1),
        declined_by_worker=True,
        body="本人から参加しない旨の申出があったため、実施しなかった。",
    )
    assert declined.declined_by_worker
    full_support(EmploymentContract.objects.get(worker=worker).host_organization)
    dates = periodic_report.support_overview(2026).items[worker.pk][SupportItem.EXCHANGE]
    assert date(2026, 6, 1) in dates and date(2026, 7, 1) not in dates  # 実施日に数えない


def test_consultation_interpreter(worker: Any) -> None:
    case = consultations.create_consultation(
        worker,
        ReceptionInput(
            record=RecordInput(
                kind=SupportItem.CONSULTATION,
                occurred_on=date(2026, 6, 1),
                method=RecordMethod.IN_PERSON,
                staff=worker.assigned_staff,
                body="寮の騒音について相談を受けた。",
            ),
            category="daily_life",
            interpreter="陳 美玲（通訳会社）",
        ),
        by=None,
        today=TODAY,
    )
    assert Consultation.objects.get(pk=case.pk).interpreter == "陳 美玲（通訳会社）"


def test_online_interview_needs_the_consent_of_the_supervisor(worker: Any) -> None:
    online: dict[str, Any] = {"method": "online", "meeting_url": "https://example.com/meeting"}
    data = interview(
        worker,
        supervisor_row=row(worker, counterpart_name="工場長 田中", **online),
    )
    without = support_interviews.create_interview(worker, data, by=None, today=TODAY)
    assert any(
        "consent" in str(w) or "同意" in str(w) for w in support_interviews.warnings(without)
    )
    data = interview(
        worker,
        on=date(2026, 9, 1),
        supervisor_row=row(worker, date(2026, 9, 1), counterpart_name="工場長 田中", **online),
    )
    consent = ContentFile(PDF_BYTES, name="同意書.pdf")
    with_consent = support_interviews.create_interview(
        worker, data, by=None, files={"consent": [consent]}, today=TODAY
    )
    supervisor = SupportRecord.objects.get(interview=with_consent, counterpart="supervisor")
    assert Attachment.objects.filter(
        object_id=supervisor.pk, kind=AttachmentKind.ONLINE_INTERVIEW_CONSENT
    ).exists()
    assert not any(
        "consent" in str(w) or "同意" in str(w) for w in support_interviews.warnings(with_consent)
    )
    # 2 回目からは、前の面談の同意の書面で足りる
    data = interview(
        worker,
        on=date(2026, 12, 1),
        supervisor_row=row(worker, date(2026, 12, 1), counterpart_name="工場長 田中", **online),
    )
    later = support_interviews.create_interview(worker, data, by=None, today=date(2026, 12, 1))
    assert not any(
        "consent" in str(w) or "同意" in str(w) for w in support_interviews.warnings(later)
    )


def test_retention_until_is_one_year_after_the_contract_ends(worker: Any) -> None:
    contract = EmploymentContract.objects.get(worker=worker)
    assert contract.current_ends_on == date(2027, 3, 31)
    assert books.retention_until(worker) == date(2028, 3, 31)
    assert books.retention_until(make_worker()) is None  # 契約が無い


def test_worker_detail_shows_the_retention(admin_client: Client, worker: Any) -> None:
    page = admin_client.get(f"/workers/{worker.pk}/").content.decode()
    assert 'id="worker-retention-until"' in page and "2028-03-31" in page
