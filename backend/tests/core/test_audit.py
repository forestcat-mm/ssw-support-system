"""監査ログ: 記録内容・マスク・追記専用・接続元 IP・記録漏れ。"""

import json
from datetime import timedelta
from io import StringIO

import pytest
from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.db import DatabaseError, connection, transaction
from django.db.models import Model
from django.test import Client, RequestFactory
from django.utils import timezone
from pytest_django.fixtures import Settings

from apps.core.models import (
    AppendOnlyError,
    AuditAction,
    AuditLog,
    Office,
    SupportLanguage,
    User,
)
from services import audit
from services.client_ip import client_ip
from services.users import deactivate_user, reactivate_user

from .factories import make_office, make_organization, make_staff, make_totp_device, make_user

pytestmark = pytest.mark.django_db


def _logs(action: AuditAction, target: Model | None = None) -> list[AuditLog]:
    logs = AuditLog.objects.filter(action=action)
    if target is not None:
        content_type = ContentType.objects.get_for_model(target)
        logs = logs.filter(content_type=content_type, object_id=str(target.pk))
    return list(logs)


# --- 完了条件: 詳細の閲覧が残る -------------------------------------------------


def test_viewing_organization_and_staff_details_is_logged(
    admin_client: Client, admin_user: User
) -> None:
    organization = make_organization()
    staff = make_staff()

    assert (
        admin_client.get(f"/admin/core/organization/{organization.pk}/change/").status_code == 200
    )
    assert admin_client.get(f"/admin/core/staff/{staff.pk}/change/").status_code == 200

    for target in (organization, staff):
        [log] = _logs(AuditAction.VIEW, target)
        assert log.actor == admin_user
        assert log.actor_username == "admin"
        assert log.ip_address == "127.0.0.1"
        assert log.object_repr == str(target)
        assert log.path.endswith("/change/")


def test_read_only_view_by_support_staff_is_logged(
    support_staff_client: Client, support_staff_user: User
) -> None:
    staff = make_staff()
    assert support_staff_client.get(f"/admin/core/staff/{staff.pk}/change/").status_code == 200
    [log] = _logs(AuditAction.VIEW, staff)
    assert log.actor == support_staff_user


def test_list_pages_and_non_personal_models_are_not_logged_as_view(admin_client: Client) -> None:
    office = make_office()
    admin_client.get("/admin/core/staff/")
    admin_client.get(f"/admin/core/office/{office.pk}/change/")
    assert _logs(AuditAction.VIEW) == []


# --- 作成・更新・削除 ------------------------------------------------------------


def test_admin_create_and_update_record_only_changed_fields(
    admin_client: Client, admin_user: User
) -> None:
    form = {
        "name": "本社",
        "postal_code": "100-0001",
        "address": "東京都千代田区",
        "is_active": "on",
    }
    assert admin_client.post("/admin/core/office/add/", form).status_code == 302
    office = Office.objects.get()
    [created] = _logs(AuditAction.CREATE, office)
    assert created.actor == admin_user
    assert created.changes["name"] == [None, "本社"]
    assert "created_at" not in created.changes

    form["address"] = "東京都港区"
    assert admin_client.post(f"/admin/core/office/{office.pk}/change/", form).status_code == 302
    [updated] = _logs(AuditAction.UPDATE, office)
    assert updated.changes == {"address": ["東京都千代田区", "東京都港区"]}


def test_writes_outside_requests_are_logged_without_actor() -> None:
    office = Office.objects.create(name="支社", postal_code="1", address="x")
    [log] = _logs(AuditAction.CREATE, office)
    assert log.actor is None
    assert log.ip_address is None
    assert log.path == ""


def test_delete_keeps_last_values() -> None:
    office = make_office(name="閉鎖予定")
    office_pk = office.pk
    office.delete()
    log = AuditLog.objects.get(action=AuditAction.DELETE, object_id=str(office_pk))
    assert log.object_repr == "閉鎖予定"
    assert log.changes["name"] == ["閉鎖予定", None]


def test_many_to_many_change_is_logged() -> None:
    staff = make_staff()
    staff.languages.set(SupportLanguage.objects.filter(code="ja"))
    staff.languages.set(SupportLanguage.objects.filter(code__in=["ja", "zh-hans"]))
    changes = [log.changes["languages"] for log in _logs(AuditAction.UPDATE, staff)]
    assert [[], ["日本語"]] in changes
    assert [["日本語"], ["中国語（簡体字）", "日本語"]] in changes


# --- マスク ----------------------------------------------------------------------


def test_password_is_masked() -> None:
    user = make_user("tanto")
    user.set_password("another-secret-password-1")
    user.save()
    [log] = _logs(AuditAction.UPDATE, user)
    assert log.changes == {"password": [audit.MASK, audit.MASK]}
    assert user.password not in json.dumps(list(AuditLog.objects.values_list("changes", flat=True)))


def test_fields_marked_audit_mask_are_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    """T06 の暗号化列は列の型に audit_mask = True を持たせる。値を残さない。"""
    monkeypatch.setattr(Office._meta.get_field("address"), "audit_mask", True, raising=False)
    office = make_office(address="平文にしてはいけない住所")
    office.address = "別の住所"
    office.save()
    created, updated = _logs(AuditAction.CREATE, office)[0], _logs(AuditAction.UPDATE, office)[0]
    assert created.changes["address"] == [None, audit.MASK]
    assert updated.changes == {"address": [audit.MASK, audit.MASK]}
    dumped = json.dumps(
        list(AuditLog.objects.values_list("changes", flat=True)), ensure_ascii=False
    )
    assert "住所" not in dumped


# --- 追記専用 --------------------------------------------------------------------


def test_audit_log_cannot_be_updated_or_deleted_by_the_app() -> None:
    entry = audit.record(AuditAction.EXPORT, context={"rows": 1})
    entry.context = {"rows": 2}
    with pytest.raises(AppendOnlyError):
        entry.save()
    with pytest.raises(AppendOnlyError):
        entry.delete()
    with pytest.raises(AppendOnlyError):
        AuditLog.objects.filter(pk=entry.pk).update(action="view")
    with pytest.raises(AppendOnlyError):
        AuditLog.objects.filter(pk=entry.pk).delete()


@pytest.mark.parametrize(
    "sql", ["UPDATE core_auditlog SET action = 'view'", "DELETE FROM core_auditlog"]
)
def test_database_trigger_rejects_update_and_delete(sql: str) -> None:
    audit.record(AuditAction.EXPORT)
    with pytest.raises(DatabaseError, match="append-only"), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(sql)
    assert AuditLog.objects.filter(action=AuditAction.EXPORT).count() == 1


# --- 接続元 IP -------------------------------------------------------------------


def test_client_ip_ignores_cf_header_unless_trusted(settings: Settings) -> None:
    request = RequestFactory().get("/", REMOTE_ADDR="10.0.0.2", HTTP_CF_CONNECTING_IP="203.0.113.5")
    settings.TRUST_CF_CONNECTING_IP = False
    assert client_ip(request) == "10.0.0.2"
    settings.TRUST_CF_CONNECTING_IP = True
    assert client_ip(request) == "203.0.113.5"


def test_client_ip_falls_back_when_header_is_invalid(settings: Settings) -> None:
    settings.TRUST_CF_CONNECTING_IP = True
    request = RequestFactory().get("/", REMOTE_ADDR="10.0.0.2", HTTP_CF_CONNECTING_IP="not-an-ip")
    assert client_ip(request) == "10.0.0.2"
    assert client_ip(RequestFactory().get("/", REMOTE_ADDR="10.0.0.2")) == "10.0.0.2"


def test_trusted_cf_ip_is_stored_in_audit_log(admin_client: Client, settings: Settings) -> None:
    settings.TRUST_CF_CONNECTING_IP = True
    organization = make_organization()
    admin_client.get(
        f"/admin/core/organization/{organization.pk}/change/", HTTP_CF_CONNECTING_IP="2001:db8::1"
    )
    [log] = _logs(AuditAction.VIEW, organization)
    assert log.ip_address == "2001:db8::1"


# --- T04 のイベント（1 件・変更前後の値つき） -------------------------------------


def test_deactivate_and_reactivate_are_single_events_with_values(admin_user: User) -> None:
    user = make_user("tanto", roles=("support_staff",))
    deactivate_user(user, by=admin_user)
    [log] = _logs(AuditAction.DEACTIVATE, user)
    assert log.actor == admin_user
    assert log.changes["is_active"] == [True, False]
    assert log.changes["deactivated_by"] == [None, admin_user.pk]
    assert log.context["revoked_invitations"] == 0
    assert _logs(AuditAction.UPDATE, user) == []

    reactivate_user(user, by=admin_user)
    [log] = _logs(AuditAction.REACTIVATE, user)
    assert log.changes["is_active"] == [False, True]
    assert log.changes["deactivated_by"] == [admin_user.pk, None]
    assert _logs(AuditAction.UPDATE, user) == []


def test_mfa_reset_from_admin_is_logged(admin_client: Client, admin_user: User) -> None:
    user = make_user("tanto", roles=("support_staff",))
    make_totp_device(user)
    admin_client.post("/admin/core/user/", {"action": "reset_mfa", "_selected_action": [user.pk]})
    [log] = _logs(AuditAction.MFA_RESET, user)
    assert log.actor == admin_user
    assert log.context["via"] == "admin"
    assert log.changes["totp_devices"] == [1, 0]
    assert log.ip_address == "127.0.0.1"


def test_mfa_reset_from_command_is_logged_without_actor() -> None:
    user = make_user("kanri", roles=("admin",))
    make_totp_device(user)
    call_command("reset_mfa", "kanri", stdout=StringIO())
    [log] = _logs(AuditAction.MFA_RESET, user)
    assert log.actor is None
    assert log.context["via"] == "manage.py reset_mfa"


def test_invitation_is_a_single_event_and_acceptance_is_by_the_user(
    admin_client: Client, admin_user: User
) -> None:
    from django.core import mail

    from apps.core.models import Invitation, Role

    data = {
        "username": "tanto",
        "email": "tanto@example.invalid",
        "roles": [Role.objects.get(code="support_staff").pk],
        "_save": "Save",
    }
    assert admin_client.post("/admin/core/user/add/", data).status_code == 302
    user = User.objects.get(username="tanto")
    [log] = _logs(AuditAction.INVITE, user)
    assert log.actor == admin_user
    assert log.changes["roles"] == [[], ["support_staff"]]
    assert log.changes["username"] == [None, "tanto"]
    assert log.changes["password"] == [None, audit.MASK]
    assert _logs(AuditAction.CREATE, user) == []
    assert not AuditLog.objects.filter(content_type__model="invitation").exists()
    invitation = Invitation.objects.get(user=user)
    assert invitation.token_hash not in json.dumps(
        list(AuditLog.objects.values_list("changes", "context")), default=str
    )

    link = str(mail.outbox[0].body).split("http://testserver", 1)[1].split()[0]
    Client().post(
        link, {"new_password1": "a-strong-new-pass-42", "new_password2": "a-strong-new-pass-42"}
    )
    [accepted] = _logs(AuditAction.UPDATE, user)
    assert accepted.actor == user
    assert accepted.context == {"via": "invitation"}
    assert accepted.changes == {"password": [audit.MASK, audit.MASK]}


# --- 記録漏れ --------------------------------------------------------------------


def test_every_app_model_is_classified() -> None:
    """apps.* のモデルは「記録する」か「理由付きで除外」のどちらか（T07 以降の記録漏れ防止）。"""
    unclassified = [
        model._meta.label_lower
        for model in apps.get_models()
        if model.__module__.startswith("apps.")
        and model._meta.label_lower not in audit.AUDITED_MODELS
        and model._meta.label_lower not in audit.EXCLUDED_MODELS
    ]
    assert unclassified == []
    assert audit.VIEW_AUDITED_MODELS <= audit.AUDITED_MODELS


def test_record_export_helper() -> None:
    entry = audit.record_export(context={"format": "csv", "rows": 30})
    assert entry.action == AuditAction.EXPORT
    assert entry.context == {"format": "csv", "rows": 30}


def test_old_entries_can_be_recorded_with_explicit_time() -> None:
    """期間検索のテスト用。追加時に日時を指定するのは許す（更新ではない）。"""
    past = timezone.now() - timedelta(days=40)
    entry = AuditLog(action=AuditAction.EXPORT, occurred_at=past)
    entry.save()
    assert AuditLog.objects.get(pk=entry.pk).occurred_at == past
