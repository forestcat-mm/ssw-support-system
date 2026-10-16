import re
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import Client
from django_otp.plugins.otp_totp.models import TOTPDevice
from pytest_django.fixtures import Settings

from apps.core.models import RecoveryCode, RecoveryCodeDevice, User
from services import mfa, recovery_codes

from .factories import TEST_PASSWORD, make_totp_device, make_user, totp_token, verified_client

pytestmark = pytest.mark.django_db

CODE_PATTERN = re.compile(r"[2-9A-HJ-NP-Z]{4}-[2-9A-HJ-NP-Z]{4}-[2-9A-HJ-NP-Z]{4}")


def _password_only(username: str) -> Client:
    client = Client()
    response = client.post("/accounts/login/", {"username": username, "password": TEST_PASSWORD})
    assert response.status_code == 302
    return client


def test_unverified_user_is_sent_to_setup_then_verify() -> None:
    user = make_user("kanri", roles=("admin",))
    client = _password_only("kanri")
    response = client.get("/admin/core/office/")
    assert response.status_code == 302
    assert response["Location"].startswith("/accounts/mfa/setup/")

    make_totp_device(user)
    response = client.get("/admin/core/office/")
    assert response["Location"].startswith("/accounts/mfa/verify/")


def test_admin_login_page_redirects_to_accounts_login() -> None:
    response = Client().get("/admin/login/?next=/admin/core/office/")
    assert response.status_code == 302
    assert response["Location"].startswith("/accounts/login/")


def test_first_setup_flow_issues_recovery_codes(settings: Settings) -> None:
    settings.OTP_TOTP_THROTTLE_FACTOR = 0
    user = make_user("kanri", roles=("admin",))
    client = _password_only("kanri")

    page = client.get("/accounts/mfa/setup/?next=/admin/")
    assert page.status_code == 200
    assert "<svg" in page.content.decode()
    device = TOTPDevice.objects.get(user=user, confirmed=False)

    wrong = client.post("/accounts/mfa/setup/", {"token": "000000", "next": "/admin/"})
    assert wrong.status_code == 200
    assert TOTPDevice.objects.get(pk=device.pk).confirmed is False

    response = client.post("/accounts/mfa/setup/", {"token": totp_token(device), "next": "/admin/"})
    assert response.status_code == 302
    assert TOTPDevice.objects.get(pk=device.pk).confirmed is True

    shown = client.get(response["Location"]).content.decode()
    codes = CODE_PATTERN.findall(shown)
    assert len(codes) == 10
    # 再表示しない
    assert CODE_PATTERN.findall(client.get("/accounts/mfa/recovery-codes/").content.decode()) == []
    # 平文は DB に無く、ハッシュだけがある
    stored = set(RecoveryCode.objects.values_list("code_hash", flat=True))
    assert stored == {recovery_codes.hash_code(code) for code in codes}
    assert not any(code.replace("-", "") in h for code in codes for h in stored)
    assert client.get("/admin/").status_code == 200


def test_setup_is_not_reopened_once_confirmed() -> None:
    user = make_user("kanri", roles=("admin",))
    make_totp_device(user)
    client = _password_only("kanri")
    response = client.get("/accounts/mfa/setup/")
    assert response["Location"].startswith("/accounts/mfa/verify/")


def test_recovery_code_is_single_use() -> None:
    user = make_user("kanri", roles=("admin",))
    make_totp_device(user)
    codes = mfa.regenerate_recovery_codes(user)

    client = _password_only("kanri")
    response = client.post("/accounts/mfa/verify/", {"code": codes[0].lower(), "next": "/admin/"})
    assert response.status_code == 302
    assert client.get("/admin/").status_code == 200
    assert mfa.remaining_recovery_codes(user) == 9

    again = _password_only("kanri")
    reused = again.post("/accounts/mfa/verify/", {"code": codes[0], "next": "/admin/"})
    assert reused.status_code == 200
    assert again.get("/admin/").status_code == 302


def test_regenerating_invalidates_old_codes(settings: Settings) -> None:
    settings.OTP_RECOVERY_CODE_THROTTLE_FACTOR = 0  # 失敗直後の待ち時間を無くして続けて検証する
    user = make_user("kanri", roles=("admin",))
    make_totp_device(user)
    old = mfa.regenerate_recovery_codes(user)
    client = verified_client(user)
    assert client.post("/accounts/mfa/recovery-codes/").status_code == 302
    new = CODE_PATTERN.findall(client.get("/accounts/mfa/recovery-codes/").content.decode())
    assert len(new) == 10
    assert mfa.verify_second_factor(user, old[0]) is None
    assert mfa.verify_second_factor(user, new[0]) is not None


def test_recovery_codes_page_requires_verification() -> None:
    make_user("kanri", roles=("admin",))
    client = _password_only("kanri")
    assert client.get("/accounts/mfa/recovery-codes/").status_code == 302


def test_admin_resets_mfa_of_user_who_lost_device(admin_client: Client) -> None:
    target = make_user("tanto", roles=("support_staff",))
    target_client = verified_client(target)
    mfa.regenerate_recovery_codes(target)

    data = {"action": "reset_mfa", "_selected_action": [target.pk]}
    assert admin_client.post("/admin/core/user/", data).status_code == 302

    assert not TOTPDevice.objects.filter(user=target).exists()
    assert not RecoveryCodeDevice.objects.filter(user=target).exists()
    assert target_client.get("/admin/").status_code == 302  # セッションが切れた
    response = _password_only("tanto").get("/admin/")
    assert response["Location"].startswith("/accounts/mfa/setup/")


def test_reset_mfa_command() -> None:
    user = make_user("kanri", roles=("admin",))
    make_totp_device(user)
    call_command("reset_mfa", "kanri", stdout=StringIO())
    assert not TOTPDevice.objects.filter(user=user).exists()


def test_next_parameter_rejects_external_urls() -> None:
    user = make_user("kanri", roles=("admin",))
    device = make_totp_device(user)
    client = _password_only("kanri")
    response = client.post(
        "/accounts/mfa/verify/", {"code": totp_token(device), "next": "https://evil.example/"}
    )
    assert response.status_code == 302
    assert response["Location"] == "/"  # LOGIN_REDIRECT_URL（業務画面のトップ）


def test_totp_issuer_uses_organization_name() -> None:
    from .factories import make_organization

    user: User = make_user("kanri")
    device = mfa.pending_totp_device(user)
    assert "SSW%20Support" in mfa.totp_config_url(device)
    make_organization(name="テスト支援機関")
    assert "issuer=%E3%83%86" in mfa.totp_config_url(device)


def test_totp_enrollment_and_regeneration_are_audited() -> None:
    import json

    from apps.core.models import AuditAction, AuditLog

    user = make_user("kanri", roles=("admin",))
    client = _password_only("kanri")
    client.get("/accounts/mfa/setup/")
    device = TOTPDevice.objects.get(user=user, confirmed=False)
    response = client.post("/accounts/mfa/setup/", {"token": totp_token(device), "next": "/admin/"})
    codes = CODE_PATTERN.findall(client.get(response["Location"]).content.decode())

    enroll = AuditLog.objects.get(action=AuditAction.MFA_ENROLL)
    assert enroll.actor == user
    assert enroll.object_id == str(user.pk)
    assert enroll.changes == {"totp_devices": [0, 1], "unused_recovery_codes": [0, 10]}
    assert enroll.ip_address == "127.0.0.1"

    assert client.post("/accounts/mfa/recovery-codes/").status_code == 302
    regenerate = AuditLog.objects.get(action=AuditAction.RECOVERY_CODES_REGENERATE)
    assert regenerate.actor == user
    assert regenerate.changes == {"unused_recovery_codes": [10, 10]}

    dumped = json.dumps(list(AuditLog.objects.values_list("changes", "context")), default=str)
    assert not any(code in dumped or code.replace("-", "") in dumped for code in codes)
