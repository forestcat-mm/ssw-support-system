"""外国人の登録・編集（T13d）: サービスの検査と保存、画面の権限・エラー・二重送信・監査。"""

from datetime import date, timedelta
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.test import Client

from apps.core.models import AuditAction, AuditLog, Country, SupportLanguage, User
from apps.workers.models import ForeignWorker, ResidenceRecord, WorkerAddress, WorkerStatus
from services import workers
from tests.core.factories import make_user, verified_client
from tests.workers.factories import add_residence, make_worker

pytestmark = pytest.mark.django_db


# --- サービス ---------------------------------------------------------------------


def residence_values(**overrides: Any) -> dict[str, Any]:
    values = {
        "card_number": "ab 1234 5678 cd",
        "permitted_on": date(2026, 4, 1),
        "expires_on": date(2027, 4, 1),
    }
    return values | overrides


def test_clean_residence_normalizes_card_number() -> None:
    cleaned = workers.clean_residence(
        residence_values(), worker_id=None, worker_status=WorkerStatus.RESIDING
    )
    assert cleaned == {"card_number": "AB12345678CD"}


def test_clean_residence_requires_card_unless_before_entry() -> None:
    values = residence_values(card_number="")
    assert workers.clean_residence(values, worker_id=None, worker_status=WorkerStatus.PRE_ENTRY)
    with pytest.raises(ValidationError) as error:
        workers.clean_residence(values, worker_id=None, worker_status=WorkerStatus.RESIDING)
    assert error.value.error_dict["card_number"][0].code == "card_number_required"


def test_clean_residence_rejects_other_workers_number_with_id_only() -> None:
    other = make_worker(name_romaji="OTHER PERSON", name_kanji="別人")
    add_residence(other, card_number="AB12345678CD")
    mine = make_worker(name_romaji="ME", name_kanji="")
    with pytest.raises(ValidationError) as error:
        workers.clean_residence(
            residence_values(), worker_id=mine.pk, worker_status=WorkerStatus.RESIDING
        )
    message = error.value.error_dict["card_number"][0].messages[0]
    assert f"ID: {other.pk}" in message and "別人" not in message and "OTHER" not in message
    # 同じ人の別の行（カードの更新で同じ番号を書き直すなど）は許す
    workers.clean_residence(residence_values(), worker_id=other.pk, worker_status="residing")


def test_clean_residence_ignores_voided_rows_of_other_workers() -> None:
    other = make_worker(name_romaji="OTHER", name_kanji="")
    wrong = add_residence(other, card_number="AB12345678CD")
    workers.void_record(wrong, by=None, reason="別の人の番号を入れた")
    workers.clean_residence(residence_values(), worker_id=None, worker_status="residing")


def test_clean_residence_checks_period_and_format_together() -> None:
    with pytest.raises(ValidationError) as error:
        workers.clean_residence(
            residence_values(card_number="A123", expires_on=date(2026, 3, 1)),
            worker_id=None,
            worker_status="residing",
        )
    assert set(error.value.error_dict) == {"card_number", "expires_on"}


def test_clean_passport() -> None:
    assert workers.clean_passport(
        {"number": "e1234-5678", "issued_on": date(2020, 1, 1), "expires_on": date(2030, 1, 1)},
        worker_id=None,
    ) == {"number": "E12345678"}
    with pytest.raises(ValidationError) as error:
        workers.clean_passport({"number": "E1"}, worker_id=None)
    assert "number" in error.value.error_dict


def new_worker(**overrides: Any) -> ForeignWorker:
    values: dict[str, Any] = {
        "name_romaji": "CHEN JIE",
        "name_kanji": "陳傑",
        "nationality": Country.objects.get(code="CN"),
        "birth_date": date(1999, 1, 2),
        "sex": "male",
        "support_language": SupportLanguage.objects.get(code="zh-hans"),
    }
    return ForeignWorker(**(values | overrides))


def test_register_worker_saves_records_and_confirms_values() -> None:
    user = make_user("tanto", roles=("support_staff",))
    address = WorkerAddress(
        postal_code="100-0001", address="東京都", effective_from=date(2026, 4, 1)
    )
    worker = workers.register_worker(new_worker(), [address], by=user)
    worker.refresh_from_db()
    assert worker.confirmed_by == user and worker.confirmed_at is not None
    assert worker.created_by == user
    saved = worker.addresses.get()
    assert saved.confirmed_by == user and saved.created_by == user


def test_register_worker_rolls_back_when_a_record_fails() -> None:
    bad = ResidenceRecord(
        card_number="AB12345678CD",
        status_of_residence="ssw_1",
        permitted_on=date(2026, 4, 1),
        expires_on=date(2027, 4, 1),
        application_type="xxx-not-saved" * 10,  # 長すぎて保存できない
    )
    before = ForeignWorker.objects.count()
    with pytest.raises(Exception):  # noqa: B017, PT011
        workers.register_worker(new_worker(), [bad], by=None)
    assert ForeignWorker.objects.count() == before


def test_update_worker_rejects_stale_edit() -> None:
    worker = make_worker()
    loaded = worker.updated_at
    ForeignWorker.objects.filter(pk=worker.pk).update(name_kanji="誰かが更新")
    other_session = ForeignWorker.objects.get(pk=worker.pk)
    other_session.save()  # updated_at が進む
    worker.name_kanji = "王芳芳"
    with pytest.raises(ValidationError) as error:
        workers.update_worker(worker, by=None, loaded_updated_at=loaded)
    assert error.value.code == "stale"


# --- 画面 -----------------------------------------------------------------------


def once_token(client: Client, url: str) -> str:
    page = client.get(url)
    assert page.status_code == 200
    return str(page.context["once_token"])


def register_data(client: Client, **overrides: str) -> dict[str, str]:
    data = {
        "once_token": once_token(client, "/workers/new/"),
        "name_romaji": "CHEN JIE",
        "name_kanji": "陳傑",
        "name_native": "陈杰",
        "nationality": str(Country.objects.get(code="CN").pk),
        "birth_date": "1999-01-02",
        "sex": "male",
        "support_language": str(SupportLanguage.objects.get(code="zh-hans").pk),
        "status": "pre_entry",
    }
    return data | overrides


@pytest.fixture
def manager_client(support_staff_client: Client) -> Client:
    return support_staff_client


def test_register_minimal_worker_before_entry(manager_client: Client) -> None:
    response = manager_client.post("/workers/new/", register_data(manager_client), follow=True)
    worker = ForeignWorker.objects.get(name_romaji="CHEN JIE")
    assert response.redirect_chain[-1][0] == f"/workers/{worker.pk}/"
    assert "登録しました" in response.content.decode()
    assert not worker.residence_records.exists()
    assert AuditLog.objects.filter(
        action=AuditAction.CREATE, object_id=str(worker.pk), content_type__model="foreignworker"
    ).exists()


def test_register_with_optional_sections(manager_client: Client) -> None:
    data = register_data(
        manager_client,
        status="residing",
        **{
            "residence-application_type": "change",
            "residence-status_of_residence": "ssw_1",
            "residence-card_number": "ab1234 5678cd",
            "residence-permitted_on": "2026-04-01",
            "residence-expires_on": "2027-04-01",
            "address-effective_from": "2026-04-01",
            "address-postal_code": "100-0001",
            "address-address": "東京都千代田区",
        },
    )
    manager_client.post("/workers/new/", data)
    worker = ForeignWorker.objects.get(name_romaji="CHEN JIE")
    assert worker.residence_records.get().card_number == "AB12345678CD"
    assert worker.addresses.count() == 1
    assert not worker.passport_records.exists()  # 触れていない区分は作らない


def test_register_shows_errors_per_field_and_saves_nothing(manager_client: Client) -> None:
    other = make_worker(name_romaji="OTHER", name_kanji="")
    add_residence(other, card_number="AB12345678CD")
    data = register_data(
        manager_client,
        name_romaji="",
        status="residing",
        **{
            "residence-application_type": "change",
            "residence-status_of_residence": "ssw_1",
            "residence-card_number": "AB12345678CD",
            "residence-permitted_on": "2026-04-01",
            "residence-expires_on": "2027-04-01",
        },
    )
    page = manager_client.post("/workers/new/", data)
    assert page.status_code == 200
    html = page.content.decode()
    assert 'id="id_name_romaji-error"' in html
    assert f"ID: {other.pk}" in html and 'id="id_residence-card_number-error"' in html
    assert not ForeignWorker.objects.filter(name_kanji="陳傑").exists()


def test_double_submit_is_saved_once(manager_client: Client) -> None:
    data = register_data(manager_client)
    manager_client.post("/workers/new/", data)
    second = manager_client.post("/workers/new/", data, follow=True)
    assert ForeignWorker.objects.filter(name_romaji="CHEN JIE").count() == 1
    assert "すでに送信" in second.content.decode()


def test_edit_basic_information(manager_client: Client) -> None:
    worker = make_worker()
    page = manager_client.get(f"/workers/{worker.pk}/edit/")
    form = page.context["form"]
    data = {name: form[name].value() or "" for name in form.fields}
    data |= {"once_token": page.context["once_token"], "status": "residing"}
    data["birth_date"] = "1998-05-01"
    response = manager_client.post(f"/workers/{worker.pk}/edit/", data, follow=True)
    worker.refresh_from_db()
    assert worker.status == WorkerStatus.RESIDING
    assert "保存しました" in response.content.decode()
    assert AuditLog.objects.filter(action=AuditAction.UPDATE, object_id=str(worker.pk)).exists()


def test_add_residence_record(manager_client: Client) -> None:
    worker = make_worker(status=WorkerStatus.RESIDING)
    url = f"/workers/{worker.pk}/residence/new/"
    data = {
        "once_token": once_token(manager_client, url),
        "application_type": "extension",
        "status_of_residence": "ssw_1",
        "card_number": "ZZ98765432YY",
        "permitted_on": "2026-09-01",
        "expires_on": "2027-09-01",
    }
    response = manager_client.post(url, data)
    assert response.status_code == 302
    record = worker.residence_records.get()
    assert record.card_number == "ZZ98765432YY" and record.created_by is not None


def test_add_residence_without_card_for_residing_worker_is_rejected(
    manager_client: Client,
) -> None:
    worker = make_worker(status=WorkerStatus.RESIDING)
    url = f"/workers/{worker.pk}/residence/new/"
    data = {
        "once_token": once_token(manager_client, url),
        "application_type": "extension",
        "status_of_residence": "ssw_1",
        "card_number": "",
        "permitted_on": "2026-09-01",
        "expires_on": "2027-09-01",
    }
    page = manager_client.post(url, data)
    assert page.status_code == 200
    assert "入国前" in page.content.decode()
    assert not worker.residence_records.exists()


def test_void_record_requires_reason_and_goes_back_to_history(manager_client: Client) -> None:
    worker = make_worker()
    record = add_residence(worker)
    url = f"/workers/{worker.pk}/history/residence/{record.pk}/void/"
    confirm_page = manager_client.get(url)
    assert "AB********CD" in confirm_page.content.decode()
    page = manager_client.post(
        url, {"once_token": confirm_page.context["once_token"], "reason": ""}
    )
    assert page.status_code == 200 and 'id="id_reason-error"' in page.content.decode()
    record.refresh_from_db()
    assert record.voided_at is None

    response = manager_client.post(
        url, {"once_token": page.context["once_token"], "reason": "番号の誤り"}
    )
    assert response["Location"] == f"/workers/{worker.pk}/history/"
    record.refresh_from_db()
    assert record.void_reason == "番号の誤り"
    # 取り消し済みの行の取消画面は開けない
    assert manager_client.get(url).status_code == 404


def test_manage_screens_require_workers_manage() -> None:
    worker = make_worker()
    record = add_residence(worker)
    viewer = make_user("viewer", roles=("support_staff",))
    viewer.user_roles.get().role.permissions.filter(action="workers.manage").delete()
    client = verified_client(viewer)
    for url in (
        "/workers/new/",
        f"/workers/{worker.pk}/edit/",
        f"/workers/{worker.pk}/residence/new/",
        f"/workers/{worker.pk}/history/residence/{record.pk}/void/",
    ):
        assert client.get(url).status_code == 403, url
    html = client.get(f"/workers/{worker.pk}/").content.decode()
    assert f"/workers/{worker.pk}/edit/" not in html


def test_unknown_record_kind_is_404(manager_client: Client) -> None:
    worker = make_worker()
    assert manager_client.get(f"/workers/{worker.pk}/contracts/new/").status_code == 404


def test_admin_uses_the_same_rule_for_card_number(admin_user: User) -> None:
    from apps.workers.admin import ResidenceRecordForm as AdminResidenceForm

    worker = make_worker(status=WorkerStatus.RESIDING)
    form = AdminResidenceForm(
        data={
            "application_type": "extension",
            "status_of_residence": "ssw_1",
            "card_number": "",
            "permitted_on": "2026-09-01",
            "expires_on": (date(2026, 9, 1) + timedelta(days=365)).isoformat(),
        },
        instance=ResidenceRecord(worker=worker),
    )
    assert not form.is_valid()
    assert "card_number" in form.errors
