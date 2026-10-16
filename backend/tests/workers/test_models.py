from datetime import date

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.workers.models import (
    ApplicationType,
    ForeignWorker,
    LatestPassportRecord,
    LatestResidenceRecord,
    LatestWorkerAddress,
    LatestWorkerContact,
    ResidenceRecord,
    WorkerAddress,
    WorkerContact,
)
from services import crypto

from .factories import add_passport, add_residence, make_worker

pytestmark = pytest.mark.django_db


def _void(record: ResidenceRecord) -> None:
    ResidenceRecord.objects.filter(pk=record.pk).update(voided_at=timezone.now())


def test_worker_has_optional_login_user_and_three_names() -> None:
    worker = make_worker(name_native="王芳")
    assert worker.user is None
    assert str(worker) == "王芳"
    assert ForeignWorker.objects.get(pk=worker.pk).status == "pre_entry"


def test_latest_residence_is_newest_permitted_on_then_id() -> None:
    worker = make_worker()
    old = add_residence(worker, permitted_on=date(2024, 4, 1), expires_on=date(2025, 4, 1))
    add_residence(worker, permitted_on=date(2023, 4, 1), expires_on=date(2024, 4, 1))
    assert LatestResidenceRecord.objects.get(worker=worker).record == old

    renewed = add_residence(worker, permitted_on=date(2025, 3, 1), expires_on=date(2028, 3, 1))
    assert ForeignWorker.objects.get(pk=worker.pk).latest_residence.record == renewed

    # 同じ日の再交付（番号が変わり期限は同じ）は ID が大きいほうが最新
    reissued = add_residence(
        worker,
        card_number="XY00000000XY",
        permitted_on=date(2025, 3, 1),
        expires_on=date(2028, 3, 1),
        application_type=ApplicationType.CARD_REISSUE,
    )
    assert LatestResidenceRecord.objects.get(worker=worker).record == reissued


def test_voided_row_is_not_latest() -> None:
    worker = make_worker()
    first = add_residence(worker, permitted_on=date(2024, 4, 1))
    wrong = add_residence(worker, permitted_on=date(2025, 4, 1), card_number="ZZ99999999ZZ")
    _void(wrong)
    assert LatestResidenceRecord.objects.get(worker=worker).record == first


def test_latest_views_for_passport_address_contact() -> None:
    worker = make_worker()
    add_passport(
        worker, issued_on=date(2015, 1, 1), expires_on=date(2020, 1, 1), number="E00000001"
    )
    current = add_passport(worker, issued_on=date(2020, 1, 10), number="E00000002")
    WorkerAddress.objects.create(
        worker=worker, postal_code="100-0001", address="旧", effective_from=date(2024, 1, 1)
    )
    address = WorkerAddress.objects.create(
        worker=worker, postal_code="100-0002", address="新", effective_from=date(2025, 1, 1)
    )
    contact = WorkerContact.objects.create(
        worker=worker, phone="090", effective_from=date(2025, 1, 1)
    )
    assert LatestPassportRecord.objects.get(worker=worker).record == current
    assert LatestWorkerAddress.objects.get(worker=worker).record == address
    assert LatestWorkerContact.objects.get(worker=worker).record == contact


def test_worker_without_history_has_no_latest_row() -> None:
    worker = make_worker()
    assert not LatestResidenceRecord.objects.filter(worker=worker).exists()


# --- 同じ番号が別の人に紐づかない -------------------------------------------------


def test_same_card_number_is_allowed_for_the_same_worker() -> None:
    worker = make_worker()
    add_residence(worker, permitted_on=date(2024, 4, 1))
    add_residence(worker, permitted_on=date(2025, 4, 1))
    assert worker.residence_records.count() == 2


def test_same_card_number_for_another_worker_is_rejected_by_db() -> None:
    add_residence(make_worker())
    other = make_worker(name_romaji="LI NA", name_kanji="李娜")
    with pytest.raises(IntegrityError), transaction.atomic():
        add_residence(other, card_number="ab-12345678-cd")  # 表記ゆれでも同じ番号


def test_voided_row_does_not_block_registering_to_the_right_worker() -> None:
    wrong_worker = make_worker()
    mistake = add_residence(wrong_worker)
    _void(mistake)
    right_worker = make_worker(name_romaji="LI NA", name_kanji="李娜")
    add_residence(right_worker)
    assert right_worker.residence_records.count() == 1


def test_blank_card_numbers_never_conflict() -> None:
    add_residence(
        make_worker(), card_number="", application_type=ApplicationType.CERTIFICATE_OF_ELIGIBILITY
    )
    add_residence(make_worker(name_romaji="LI NA"), card_number="")


def test_same_passport_number_for_another_worker_is_rejected_by_db() -> None:
    add_passport(make_worker())
    with pytest.raises(IntegrityError), transaction.atomic():
        add_passport(make_worker(name_romaji="LI NA"))


def test_numbers_are_encrypted_in_database() -> None:
    worker = make_worker()
    residence = add_residence(worker)
    passport = add_passport(worker)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT card_number, card_number_index FROM workers_residencerecord WHERE id = %s",
            [residence.pk],
        )
        card, card_index = cursor.fetchone()
        cursor.execute("SELECT number FROM workers_passportrecord WHERE id = %s", [passport.pk])
        (number,) = cursor.fetchone()
    assert card.startswith("gAAAA") and "AB12345678CD" not in card
    assert number.startswith("gAAAA") and "E12345678" not in number
    assert card_index == crypto.blind_index("AB12345678CD")
