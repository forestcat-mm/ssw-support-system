import pytest
from axes.models import AccessAttempt
from django.conf import settings
from django.test import Client

from .factories import TEST_PASSWORD, make_totp_device, make_user

pytestmark = pytest.mark.django_db


def _login(client: Client, username: str, password: str) -> int:
    response = client.post("/accounts/login/", {"username": username, "password": password})
    return response.status_code


def test_lockout_settings_come_from_environment_defaults() -> None:
    assert settings.AXES_FAILURE_LIMIT == 5
    assert settings.AXES_COOLOFF_TIME.total_seconds() == 30 * 60
    assert settings.AXES_LOCKOUT_PARAMETERS == ["username"]


def test_five_failures_lock_the_account() -> None:
    make_user("tanto", roles=("support_staff",))
    client = Client()
    for _ in range(4):
        assert _login(client, "tanto", "wrong-password") == 200
    assert _login(client, "tanto", "wrong-password") == 429  # 5 回目でロック
    response = client.post("/accounts/login/", {"username": "tanto", "password": TEST_PASSWORD})
    assert response.status_code == 429
    assert "一時的にロック" in response.content.decode()
    assert "_auth_user_id" not in client.session
    # 別ユーザーには影響しない
    make_user("other", roles=("support_staff",))
    assert _login(Client(), "other", TEST_PASSWORD) == 302


def test_wrong_second_factor_counts_toward_lockout() -> None:
    user = make_user("tanto", roles=("support_staff",))
    make_totp_device(user)
    client = Client()
    assert _login(client, "tanto", TEST_PASSWORD) == 302
    statuses = [
        client.post("/accounts/mfa/verify/", {"code": "000000"}).status_code for _ in range(5)
    ]
    assert statuses[-1] == 429
    assert "_auth_user_id" not in client.session  # ロック時はログアウトさせる
    assert _login(Client(), "tanto", TEST_PASSWORD) == 429


def test_admin_unlocks_account(admin_client: Client) -> None:
    make_user("tanto", roles=("support_staff",))
    for _ in range(5):
        _login(Client(), "tanto", "wrong-password")
    attempt = AccessAttempt.objects.get(username="tanto")
    data = {"action": "delete_selected", "_selected_action": [attempt.pk], "post": "yes"}
    assert admin_client.post("/admin/axes/accessattempt/", data).status_code == 302
    assert not AccessAttempt.objects.filter(username="tanto").exists()
    assert _login(Client(), "tanto", TEST_PASSWORD) == 302
