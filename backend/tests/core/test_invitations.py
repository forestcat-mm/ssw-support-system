import re
from datetime import timedelta

import pytest
from django.core import mail
from django.test import Client
from django.utils import timezone
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.core.models import Invitation, Role, Staff, User
from services.invitations import InvitationStatus, invitation_status
from services.users import deactivate_user

from .factories import make_staff, totp_token

pytestmark = pytest.mark.django_db

NEW_PASSWORD = "a-strong-new-password-42"
LINK = re.compile(r"http://testserver(/accounts/invitations/[\w-]+/)")


def _invite(admin_client: Client, **overrides: object) -> User:
    data: dict[str, object] = {
        "username": "tanto",
        "email": "tanto@example.invalid",
        "last_name": "山田",
        "first_name": "花子",
        "roles": [Role.objects.get(code="support_staff").pk],
        "staff": "",
        "_save": "Save",
    }
    data |= overrides
    response = admin_client.post("/admin/core/user/add/", data)
    assert response.status_code == 302
    return User.objects.get(username=data["username"])


def _link(index: int = -1) -> str:
    match = LINK.search(str(mail.outbox[index].body))
    assert match is not None
    return match.group(1)


def test_admin_invites_user_and_email_is_sent(admin_client: Client, admin_user: User) -> None:
    staff = make_staff(user=None)
    user = _invite(admin_client, staff=staff.pk)

    assert not user.has_usable_password()
    assert list(user.user_roles.values_list("role__code", flat=True)) == ["support_staff"]
    assert Staff.objects.get(pk=staff.pk).user == user
    invitation = Invitation.objects.get(user=user)
    assert invitation.created_by == admin_user
    assert invitation_status(user) is InvitationStatus.PENDING

    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.to == ["tanto@example.invalid"]
    token = _link().rstrip("/").rsplit("/", 1)[-1]
    assert token not in invitation.token_hash  # 平文のトークンは保存しない
    assert message.body.startswith("山田 花子 様")


def test_invited_user_sets_password_then_totp_and_has_staff_permissions(
    admin_client: Client,
) -> None:
    user = _invite(admin_client)
    client = Client()
    assert client.get(_link()).status_code == 200

    mismatch = client.post(_link(), {"new_password1": NEW_PASSWORD, "new_password2": "other"})
    assert mismatch.status_code == 200
    response = client.post(_link(), {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD})
    assert response.status_code == 302
    assert response["Location"] == "/accounts/mfa/setup/"

    client.get("/accounts/mfa/setup/")
    device = TOTPDevice.objects.get(user=user, confirmed=False)
    response = client.post("/accounts/mfa/setup/", {"token": totp_token(device), "next": "/admin/"})
    assert response.status_code == 302

    assert client.get("/admin/core/office/").status_code == 200
    assert client.get("/admin/core/user/").status_code == 403
    user.refresh_from_db()
    assert user.check_password(NEW_PASSWORD)
    assert invitation_status(user) is InvitationStatus.ACCEPTED


def test_link_is_single_use(admin_client: Client) -> None:
    _invite(admin_client)
    link = _link()
    Client().post(link, {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD})
    assert Client().get(link).status_code == 400


def test_expired_link_cannot_be_used(admin_client: Client) -> None:
    user = _invite(admin_client)
    Invitation.objects.filter(user=user).update(expires_at=timezone.now() - timedelta(seconds=1))
    assert Client().get(_link()).status_code == 400
    assert invitation_status(user) is InvitationStatus.EXPIRED


def test_resend_invalidates_previous_link(admin_client: Client, admin_user: User) -> None:
    user = _invite(admin_client)
    old = _link()
    data = {"action": "resend_invitation", "_selected_action": [user.pk]}
    assert admin_client.post("/admin/core/user/", data).status_code == 302
    assert len(mail.outbox) == 2
    assert Client().get(old).status_code == 400
    assert Client().get(_link()).status_code == 200


def test_resend_skips_users_who_already_set_password(
    admin_client: Client, admin_user: User
) -> None:
    data = {"action": "resend_invitation", "_selected_action": [admin_user.pk]}
    assert admin_client.post("/admin/core/user/", data).status_code == 302
    assert mail.outbox == []


def test_deactivation_revokes_pending_invitation(admin_client: Client, admin_user: User) -> None:
    user = _invite(admin_client)
    deactivate_user(user, by=admin_user)
    assert Client().get(_link()).status_code == 400
    assert Invitation.objects.get(user=user).revoked_at is not None


def test_email_is_required_and_unique(admin_client: Client) -> None:
    _invite(admin_client)
    data = {
        "username": "other",
        "email": "TANTO@example.invalid",
        "roles": [Role.objects.get(code="support_staff").pk],
    }
    response = admin_client.post("/admin/core/user/add/", data)
    assert response.status_code == 200
    assert not User.objects.filter(username="other").exists()
    response = admin_client.post("/admin/core/user/add/", data | {"email": ""})
    assert response.status_code == 200


def test_support_staff_cannot_invite(support_staff_client: Client) -> None:
    assert support_staff_client.get("/admin/core/user/add/").status_code == 403
