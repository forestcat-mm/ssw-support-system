import os
from io import StringIO

import pytest
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models import ProtectedError
from django.test import Client

from apps.core.models import Staff, User
from services.users import LastAdminError, deactivate_user, reactivate_user

from .factories import TEST_PASSWORD, make_staff, make_user, verified_client

pytestmark = pytest.mark.django_db


def _can_sign_in(username: str) -> bool:
    response = Client().post("/accounts/login/", {"username": username, "password": TEST_PASSWORD})
    return response.status_code == 302


def test_deactivate_keeps_records_and_ends_sessions(admin_user: User) -> None:
    retiree = make_user("retiree", roles=("support_staff",))
    staff = make_staff(user=retiree)
    client = verified_client(retiree)
    assert client.get("/admin/").status_code == 200

    deactivate_user(retiree, by=admin_user)

    retiree.refresh_from_db()
    assert retiree.is_active is False
    assert retiree.deactivated_by == admin_user
    assert retiree.deactivated_at is not None
    # セッションが消えて即時ログアウト
    assert client.get("/admin/").status_code == 302
    assert not any(
        s.get_decoded().get("_auth_user_id") == str(retiree.pk) for s in Session.objects.all()
    )
    # 再ログインできない
    assert not _can_sign_in("retiree")
    # Staff は紐づいたまま残る（Staff の is_active は別運用）
    assert Staff.objects.get(pk=staff.pk).user == retiree
    # User は削除できない（Staff から PROTECT）
    with pytest.raises(ProtectedError):
        retiree.delete()


def test_reactivate(admin_user: User) -> None:
    user = make_user("back", roles=("support_staff",))
    deactivate_user(user, by=admin_user)
    reactivate_user(user, by=admin_user)
    user.refresh_from_db()
    assert user.is_active is True
    assert user.deactivated_at is None
    assert _can_sign_in("back")


def test_last_active_admin_cannot_be_deactivated(admin_user: User) -> None:
    with pytest.raises(LastAdminError):
        deactivate_user(admin_user, by=admin_user)
    other = make_user("admin2", roles=("admin",))
    deactivate_user(admin_user, by=other)
    assert User.objects.get(pk=admin_user.pk).is_active is False


def test_admin_actions_deactivate_and_reactivate(admin_client: Client) -> None:
    user = make_user("target", roles=("support_staff",))
    data = {"action": "deactivate", "_selected_action": [user.pk]}
    assert admin_client.post("/admin/core/user/", data).status_code == 302
    assert User.objects.get(pk=user.pk).is_active is False
    data["action"] = "reactivate"
    assert admin_client.post("/admin/core/user/", data).status_code == 302
    assert User.objects.get(pk=user.pk).is_active is True


def test_admin_cannot_delete_users_or_edit_is_active(admin_client: Client) -> None:
    user = make_user("target", roles=("support_staff",))
    assert admin_client.get(f"/admin/core/user/{user.pk}/delete/").status_code == 403
    response = admin_client.get(f"/admin/core/user/{user.pk}/change/")
    assert "is_active" not in response.context["adminform"].form.fields


def test_create_admin_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(os.environ, "CREATE_ADMIN_PASSWORD", TEST_PASSWORD)
    out = StringIO()
    call_command(
        "create_admin",
        "--username",
        "boss",
        "--email",
        "b@example.invalid",
        "--noinput",
        stdout=out,
    )
    user = User.objects.get(username="boss")
    assert user.is_superuser is False
    assert list(user.user_roles.values_list("role__code", flat=True)) == ["admin"]


def test_create_admin_command_validates_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(os.environ, "CREATE_ADMIN_PASSWORD", "123")
    with pytest.raises(CommandError):
        call_command("create_admin", "--username", "weak", "--noinput")
    assert not User.objects.filter(username="weak").exists()
