"""列暗号化・ブラインドインデックス・鍵のローテーション（T06）。"""

import json
from io import StringIO

import pytest
from cryptography.fernet import Fernet, InvalidToken
from django import forms
from django.core.exceptions import FieldError
from django.core.management import call_command
from django.db import connection
from django_otp.plugins.otp_totp.models import TOTPDevice
from pytest_django.fixtures import Settings

from apps.core.models import AuditAction, AuditLog
from services import audit, crypto
from tests.testapp.models import SecretRecord

from .factories import make_totp_device, make_user

pytestmark = pytest.mark.django_db

CARD = "AB12345678CD"
ACCOUNT = "三菱UFJ 普通 1234567"


def _raw(pk: int) -> dict[str, str | None]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT card_number, card_number_index, bank_account FROM testapp_secretrecord "
            "WHERE id = %s",
            [pk],
        )
        card, index, account = cursor.fetchone()
    return {"card_number": card, "card_number_index": index, "bank_account": account}


def test_database_does_not_contain_plaintext() -> None:
    record = SecretRecord.objects.create(card_number=CARD, bank_account=ACCOUNT)
    raw = _raw(record.pk)
    assert raw["card_number"] is not None and raw["card_number"].startswith("gAAAA")
    assert raw["bank_account"] is not None and raw["bank_account"].startswith("gAAAA")
    for value in raw.values():
        assert CARD not in str(value)
        assert "1234567" not in str(value)
    assert raw["card_number_index"] == crypto.blind_index(CARD)
    assert raw["card_number_index"] != CARD


def test_model_reads_plaintext_back() -> None:
    record = SecretRecord.objects.create(card_number=CARD, bank_account=ACCOUNT)
    loaded = SecretRecord.objects.get(pk=record.pk)
    assert loaded.card_number == CARD
    assert loaded.bank_account == ACCOUNT


def test_empty_and_null_values() -> None:
    record = SecretRecord.objects.create(card_number="", bank_account=None)
    assert _raw(record.pk) == {"card_number": "", "card_number_index": None, "bank_account": None}
    loaded = SecretRecord.objects.get(pk=record.pk)
    assert loaded.card_number == ""
    assert loaded.bank_account is None
    assert SecretRecord.objects.filter(bank_account__isnull=True).count() == 1


def test_same_value_encrypts_differently_each_time() -> None:
    a = SecretRecord.objects.create(card_number=CARD)
    b = SecretRecord.objects.create(card_number=CARD)
    assert _raw(a.pk)["card_number"] != _raw(b.pk)["card_number"]
    assert _raw(a.pk)["card_number_index"] == _raw(b.pk)["card_number_index"]


def test_lookups_on_encrypted_columns_are_rejected() -> None:
    with pytest.raises(FieldError):
        SecretRecord.objects.filter(card_number=CARD).exists()
    with pytest.raises(FieldError):
        SecretRecord.objects.filter(card_number__icontains="1234").exists()


def test_form_field_validates_plaintext_length() -> None:
    form_field = SecretRecord._meta.get_field("card_number").formfield()
    assert isinstance(form_field, forms.CharField)
    assert form_field.max_length == 12
    assert form_field.widget.__class__.__name__ == "TextInput"


# --- ブラインドインデックスで検索 ---------------------------------------------------


@pytest.mark.parametrize(
    "query", [CARD, "ab12345678cd", "AB 1234 5678 CD", "AB-12345678-CD", "ＡＢ１２３４５６７８ＣＤ"]
)
def test_search_by_blind_index_absorbs_notation(query: str) -> None:
    record = SecretRecord.objects.create(card_number=CARD)
    SecretRecord.objects.create(card_number="ZZ99999999ZZ")
    found = SecretRecord.objects.filter(card_number_index=crypto.blind_index(query))
    assert list(found) == [record]


def test_blind_index_follows_updates() -> None:
    record = SecretRecord.objects.create(card_number=CARD)
    record.card_number = "XY00000000XY"
    record.save()
    assert _raw(record.pk)["card_number_index"] == crypto.blind_index("XY00000000XY")


def test_blind_index_depends_on_key(settings: Settings) -> None:
    before = crypto.blind_index(CARD)
    settings.BLIND_INDEX_KEY = crypto.generate_key()
    assert crypto.blind_index(CARD) != before


# --- 監査ログに平文が残らない -----------------------------------------------------


def test_audit_log_masks_encrypted_and_index_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit, "AUDITED_MODELS", audit.AUDITED_MODELS | {"testapp.secretrecord"})
    record = SecretRecord.objects.create(card_number=CARD, bank_account=ACCOUNT, note="メモ")
    record.card_number = "XY00000000XY"
    record.save()

    created = AuditLog.objects.get(action=AuditAction.CREATE, object_id=str(record.pk))
    assert created.changes["card_number"] == [None, audit.MASK]
    assert created.changes["card_number_index"] == [None, audit.MASK]
    assert created.changes["bank_account"] == [None, audit.MASK]
    assert created.changes["note"] == [None, "メモ"]
    updated = AuditLog.objects.get(action=AuditAction.UPDATE, object_id=str(record.pk))
    assert updated.changes == {
        "card_number": [audit.MASK, audit.MASK],
        "card_number_index": [audit.MASK, audit.MASK],
    }
    dumped = json.dumps(
        list(AuditLog.objects.values_list("changes", flat=True)), ensure_ascii=False
    )
    for secret in (CARD, "XY00000000XY", "1234567", "gAAAA", str(crypto.blind_index(CARD))):
        assert secret not in dumped


# --- 鍵のローテーション -----------------------------------------------------------


def test_rotation_reencrypts_rows_written_with_old_key(settings: Settings) -> None:
    old_key = settings.FIELD_ENCRYPTION_KEYS[0]
    record = SecretRecord.objects.create(card_number=CARD, bank_account=ACCOUNT)
    old_token = _raw(record.pk)["card_number"]

    new_key = crypto.generate_key()
    settings.FIELD_ENCRYPTION_KEYS = [new_key, old_key]
    assert SecretRecord.objects.get(pk=record.pk).card_number == CARD  # 旧鍵でも読める

    out = StringIO()
    call_command("rotate_encryption_keys", "--check", stdout=out)
    assert "rows on old keys: 2" in out.getvalue()

    call_command("rotate_encryption_keys", stdout=StringIO())
    assert _raw(record.pk)["card_number"] != old_token

    settings.FIELD_ENCRYPTION_KEYS = [new_key]  # 旧鍵を外しても読める
    loaded = SecretRecord.objects.get(pk=record.pk)
    assert (loaded.card_number, loaded.bank_account) == (CARD, ACCOUNT)
    out = StringIO()
    call_command("rotate_encryption_keys", "--check", stdout=out)
    assert "rows on old keys: 0" in out.getvalue()

    log = AuditLog.objects.get(action=AuditAction.ENCRYPTION_KEY_ROTATION)
    assert log.context["rotated"]["testapp.secretrecord.card_number"] == 1
    assert log.context["rotated"]["testapp.secretrecord.bank_account"] == 1


def test_removing_key_too_early_fails_loudly(settings: Settings) -> None:
    record = SecretRecord.objects.create(card_number=CARD)
    settings.FIELD_ENCRYPTION_KEYS = [crypto.generate_key()]
    with pytest.raises(InvalidToken):
        SecretRecord.objects.get(pk=record.pk)


def test_rebuild_blind_indexes_after_key_change(settings: Settings) -> None:
    record = SecretRecord.objects.create(card_number=CARD)
    settings.BLIND_INDEX_KEY = crypto.generate_key()
    assert not SecretRecord.objects.filter(card_number_index=crypto.blind_index(CARD)).exists()

    call_command("rebuild_blind_indexes", stdout=StringIO())

    assert list(SecretRecord.objects.filter(card_number_index=crypto.blind_index(CARD))) == [record]
    log = AuditLog.objects.get(action=AuditAction.BLIND_INDEX_REBUILD)
    assert log.context["changed"]["testapp.secretrecord.card_number_index"] == 1


# --- 鍵の設定チェック -------------------------------------------------------------


def test_invalid_keys_are_reported(settings: Settings) -> None:
    assert crypto.check_configuration() == []
    settings.FIELD_ENCRYPTION_KEYS = ["REPLACE_ME"]
    settings.BLIND_INDEX_KEY = "short"
    problems = crypto.check_configuration()
    assert len(problems) == 2


def test_generated_key_is_a_valid_fernet_key() -> None:
    Fernet(crypto.generate_key().encode())


# --- 漏洩時の一括 MFA リセット ------------------------------------------------------


def test_reset_mfa_all_requires_confirmation_and_logs_each_user() -> None:
    from django.core.management.base import CommandError

    users = [make_user(name, roles=("support_staff",)) for name in ("a", "b")]
    for user in users:
        make_totp_device(user)
    with pytest.raises(CommandError):
        call_command("reset_mfa", "--all", stdout=StringIO())
    assert TOTPDevice.objects.count() == 2

    call_command("reset_mfa", "--all", "--yes", stdout=StringIO())
    assert TOTPDevice.objects.count() == 0
    logs = AuditLog.objects.filter(action=AuditAction.MFA_RESET)
    assert {log.object_id for log in logs} == {str(user.pk) for user in users}
    assert {log.context["via"] for log in logs} == {"manage.py reset_mfa"}
