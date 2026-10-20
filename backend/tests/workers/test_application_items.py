"""一括申請の欄から逆算して足した外国人の項目（T46a。ADR-0038）。"""

from typing import Any

import pytest
from django.test import Client

from apps.core.models import Attachment, AttachmentKind, AuditAction, AuditLog
from apps.workers.models import (
    StatusOfResidence,
    WorkerAddress,
    WorkerQualification,
    WorkerRelative,
    WorkerStatus,
    WorkHistory,
)
from services import addresses, audit, immigration_codes, workers
from tests.conftest import PDF_BYTES
from tests.workers.factories import make_worker
from tests.workers.test_worker_forms import once_token

pytestmark = pytest.mark.django_db


@pytest.fixture
def manager_client(support_staff_client: Client) -> Client:
    return support_staff_client


# --- ツールの選択肢の表（T46-D3）------------------------------------------------------------


def test_code_tables_come_from_the_tool_data() -> None:
    assert immigration_codes.version() == "2026-06-04"
    assert immigration_codes.label(immigration_codes.COUNTRY, "039") == "中国"
    assert immigration_codes.label(immigration_codes.COUNTRY, "900") == "日本"
    assert immigration_codes.japanese("日本語能力試験 Japanese language proficiency test") == (
        "日本語能力試験"
    )
    evaluations = dict(immigration_codes.choices(immigration_codes.EVALUATION))
    assert evaluations["03"] == "特定技能評価試験"
    # 技能実習の作業は職種ごとの連動の表
    works = dict(immigration_codes.linked(immigration_codes.INTERN_OCCUPATION, "L01"))
    assert set(works) == {"A1", "A2", "A3"}
    # 使う表がすべてある
    for code in (
        immigration_codes.RELATIONSHIP,
        immigration_codes.SKILL_EXAM,
        immigration_codes.JAPANESE_EXAM,
        immigration_codes.INTERN_WORK,
        immigration_codes.INTERN_PROOF,
        immigration_codes.PERIOD_OF_STAY,
        immigration_codes.PREFECTURE,
    ):
        assert immigration_codes.table(code), code


# --- 住所の分割（T46-D4）-------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("千葉県船橋市本町1-2-3", ("120006", "122041", "本町1-2-3")),
        ("北海道当別町白樺1", ("010006", "013030", "白樺1")),  # 郡を書かない
        ("北海道石狩郡当別町白樺1", ("010006", "013030", "白樺1")),
        ("奈良県大和郡山市北郡山町1", ("290009", "292036", "北郡山町1")),  # 市の名前の「郡」
        ("東京都府中市宮西町1", ("130001", "132063", "宮西町1")),
        ("広島県府中市府川町1", ("340006", "342084", "府川町1")),
    ],
)
def test_address_is_split_by_the_longest_match(address: str, expected: tuple[str, ...]) -> None:
    split = addresses.split(address)
    assert (split.prefecture_code, split.municipality_code, split.rest) == expected


@pytest.mark.parametrize("address", ["愛知県サンプル市ダミー町1-1", "船橋市本町1", ""])
def test_address_that_cannot_be_split_is_left_empty(address: str) -> None:
    assert addresses.split(address) == addresses.EMPTY


def test_new_address_rows_keep_the_split(manager_client: Client) -> None:
    worker = make_worker(status=WorkerStatus.RESIDING)
    url = f"/workers/{worker.pk}/address/new/"
    response = manager_client.post(
        url,
        {
            "once_token": once_token(manager_client, url),
            "postal_code": "273-0005",
            "address": "千葉県船橋市本町1-2-3 サンプル寮101",
            "effective_from": "2026-09-01",
        },
    )
    assert response.status_code == 302
    address = WorkerAddress.objects.get(worker=worker)
    assert (address.prefecture_code, address.municipality_code) == ("120006", "122041")
    page = manager_client.get(f"/workers/{worker.pk}/").content.decode()
    assert 'id="address-split"' in page and "船橋市" in page


# --- 在留情報の在留期間・技能実習のイ・ロ ---------------------------------------------------


def test_residence_keeps_period_and_intern_type(manager_client: Client) -> None:
    worker = make_worker(status=WorkerStatus.RESIDING)
    url = f"/workers/{worker.pk}/residence/new/"
    data = {
        "once_token": once_token(manager_client, url),
        "application_type": "change",
        "status_of_residence": StatusOfResidence.SSW_1,
        "technical_intern_type": "i",
        "card_number": "ZZ98765432YY",
        "permitted_on": "2026-09-01",
        "expires_on": "2027-09-01",
        "period_of_stay": "0100000",
    }
    page = manager_client.post(url, data)
    assert page.status_code == 200  # 特定技能の行にイ・ロは選べない
    assert "technical_intern_type" in page.context["form"].errors
    data |= {"once_token": once_token(manager_client, url), "technical_intern_type": ""}
    response = manager_client.post(url, data)
    assert response.status_code == 302, response.context["form"].errors
    record = worker.residence_records.get()
    assert record.period_of_stay == "0100000"
    assert immigration_codes.label(immigration_codes.PERIOD_OF_STAY, "0100000") == "１年"


# --- 在日親族・試験・技能実習・職歴 ---------------------------------------------------------


def test_relative_card_number_is_encrypted_and_masked(manager_client: Client) -> None:
    worker = make_worker(status=WorkerStatus.RESIDING)
    url = f"/workers/{worker.pk}/relative/new/"
    response = manager_client.post(
        url,
        {
            "once_token": once_token(manager_client, url),
            "relationship": "02",
            "name": "WANG LI",
            "birth_date_year": "1997",
            "birth_date_month": "3",
            "birth_date_day": "4",
            "nationality_code": "039",
            "lives_together": "on",
            "workplace": "株式会社サンプル",
            "card_number": "AB12345678CD",
        },
    )
    assert response.status_code == 302
    relative = WorkerRelative.objects.get(worker=worker)
    assert relative.card_number == "AB12345678CD"
    log = AuditLog.objects.get(action=AuditAction.CREATE, object_id=str(relative.pk))
    assert log.changes["card_number"] == [None, audit.MASK]  # 監査ログに番号を残さない
    page = manager_client.get(f"/workers/{worker.pk}/").content.decode()
    assert "AB********CD" in page and "AB12345678CD" not in page
    history = manager_client.get(f"/workers/{worker.pk}/history/").content.decode()
    assert 'data-kind="relative"' in history


def test_qualification_with_a_certificate(manager_client: Client) -> None:
    from django.core.files.uploadedfile import SimpleUploadedFile

    worker = make_worker(status=WorkerStatus.RESIDING)
    url = f"/workers/{worker.pk}/qualification/new/"
    values: dict[str, Any] = {
        "area": "japanese",
        "evaluation": "03",
        "exam": "003",  # 技能の試験のコード（日本語の分野には選べない）
        "exam_country": "039",
        "passed_on": "2025-12-01",
        "level": "N4",
    }
    page = manager_client.post(url, {"once_token": once_token(manager_client, url), **values})
    assert "exam" in page.context["form"].errors
    values["exam"] = "ja:003"
    certificate = SimpleUploadedFile("合格証明書.pdf", PDF_BYTES, content_type="application/pdf")
    response = manager_client.post(
        url,
        {"once_token": once_token(manager_client, url), **values, "certificate": certificate},
    )
    assert response.status_code == 302
    row = WorkerQualification.objects.get(worker=worker)
    assert (row.exam, row.level) == ("003", "N4")
    assert Attachment.objects.filter(
        object_id=row.pk, kind=AttachmentKind.QUALIFICATION_CERTIFICATE
    ).exists()
    page_text = manager_client.get(f"/workers/{worker.pk}/").content.decode()
    assert "日本語能力試験" in page_text and "合格証明書.pdf" in page_text


def test_internship_work_must_belong_to_the_job_category(manager_client: Client) -> None:
    worker = make_worker(status=WorkerStatus.RESIDING)
    url = f"/workers/{worker.pk}/internship/new/"
    values = {"occupation": "L01", "work": "A4", "proof": "01", "completed_on": "2025-03-31"}
    page = manager_client.post(url, {"once_token": once_token(manager_client, url), **values})
    assert "work" in page.context["form"].errors
    values["work"] = "A1"
    response = manager_client.post(url, {"once_token": once_token(manager_client, url), **values})
    assert response.status_code == 302


def test_work_history_keeps_only_the_month(manager_client: Client) -> None:
    worker = make_worker(status=WorkerStatus.RESIDING)
    url = f"/workers/{worker.pk}/work/new/"
    response = manager_client.post(
        url,
        {
            "once_token": once_token(manager_client, url),
            "joined_on": "2019-04-15",
            "left_on": "2022-03-20",
            "employer": "サンプル食品有限公司",
        },
    )
    assert response.status_code == 302
    row = WorkHistory.objects.get(worker=worker)
    assert (row.joined_on.day, row.left_on and row.left_on.day) == (1, 1)


def test_rows_are_voided_on_the_history_tab(manager_client: Client) -> None:
    worker = make_worker(status=WorkerStatus.RESIDING)
    row = workers.add_record(
        worker, WorkHistory(joined_on="2019-04-01", employer="誤った勤務先"), by=None
    )
    url = f"/workers/{worker.pk}/history/work/{row.pk}/void/"
    response = manager_client.post(
        url, {"once_token": once_token(manager_client, url), "reason": "入力の誤り"}
    )
    assert response.status_code == 302
    row.refresh_from_db()
    assert row.voided_at is not None
    page = manager_client.get(f"/workers/{worker.pk}/").content.decode()
    assert "誤った勤務先" not in page


def test_edit_form_has_the_new_worker_items(manager_client: Client) -> None:
    worker = make_worker()
    page = manager_client.get(f"/workers/{worker.pk}/edit/")
    fields = page.context["form"].fields
    for name in (
        "birthplace",
        "has_relatives_in_japan",
        "has_work_history",
        "ssw_months_outside_ledger",
    ):
        assert name in fields
