"""パスワードの再設定（T25）: 申請（アカウントの有無で変わらない・IP ごとの上限・
ユーザーごとの間隔）、リンク（一度だけ・期限・新しい申請で無効・トークンを URL から外す）、
再設定（検査・二要素認証は変えない・監査ログ・変更の通知・ロック中の案内）。"""

import re
from datetime import timedelta
from typing import Any
from unittest import mock

import pytest
from django.core import mail
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.core.models import AuditAction, AuditLog, PasswordReset, User
from services import password_reset, users
from tests.core.factories import TEST_PASSWORD, make_totp_device, make_user

pytestmark = pytest.mark.django_db

EMAIL = "tanto@example.invalid"
NEW_PASSWORD = "kN8#vQ2!pLm4"
LINK = re.compile(r"http://testserver(/accounts/password-reset/[^/\s]+/)")


@pytest.fixture
def user() -> User:
    user = make_user("tanto", roles=("support_staff",), email=EMAIL)
    make_totp_device(user)
    return user


def request_reset(client: Client, email: str = EMAIL, ip: str = "203.0.113.5") -> Any:
    return client.post("/accounts/password-reset/", {"email": email}, REMOTE_ADDR=ip)


def link_in(message: Any) -> str:
    match = LINK.search(str(message.body))
    assert match is not None
    return match.group(1)


def set_password(client: Client, link: str, password: str = NEW_PASSWORD) -> Any:
    opened = client.get(link)
    assert opened.status_code == 302 and opened["Location"] == "/accounts/password-reset/set/"
    return client.post(
        "/accounts/password-reset/set/",
        {"new_password1": password, "new_password2": password},
    )


# --- 申請 ------------------------------------------------------------------------------


def test_login_page_links_to_the_reset() -> None:
    html = Client().get("/accounts/login/").content.decode()
    assert 'href="/accounts/password-reset/"' in html and "パスワードを忘れた場合" in html


def test_request_sends_a_link_and_is_audited(user: User) -> None:
    response = request_reset(Client())
    assert response.status_code == 302 and response["Location"] == "/accounts/password-reset/sent/"
    assert len(mail.outbox) == 1 and mail.outbox[0].to == [EMAIL]
    body = str(mail.outbox[0].body)
    assert "ユーザー名: tanto" in body and "二要素認証の設定は変わりません" in body
    requested = AuditLog.objects.get(action=AuditAction.PASSWORD_RESET_REQUEST)
    assert (requested.object_id, requested.context) == ("", {"rate_limited": False})
    assert requested.ip_address == "203.0.113.5"
    sent = AuditLog.objects.get(action=AuditAction.PASSWORD_RESET_LINK)
    assert (sent.object_id, sent.ip_address) == (str(user.pk), "203.0.113.5")
    assert EMAIL not in str(requested.context)  # 入力されたメールアドレスは残さない


@pytest.mark.parametrize("case", ["unknown", "inactive", "deactivated", "invited"])
def test_same_result_without_an_eligible_account(user: User, case: str) -> None:
    known = request_reset(Client())
    known_page = Client().get(known["Location"]).content
    mail.outbox.clear()
    email = EMAIL
    if case == "unknown":
        email = "nobody@example.invalid"
    elif case == "inactive":
        user.is_active = False
        user.save()
    elif case == "deactivated":
        users.deactivate_user(user, by=None)
    else:
        user.set_unusable_password()  # 招待の受諾前
        user.save()
    PasswordReset.objects.all().delete()  # ユーザーごとの間隔の影響を外す
    response = request_reset(Client(), email, ip="198.51.100.7")
    assert (response.status_code, response["Location"]) == (known.status_code, known["Location"])
    assert Client().get(response["Location"]).content == known_page
    assert mail.outbox == []


def test_request_does_the_same_work_with_or_without_an_account(user: User, settings: Any) -> None:
    """申請の画面ではアカウントを探さない（送信はジョブ）。アカウントの有無で同じクエリを流す。"""
    settings.JOBS_INLINE = False
    with mock.patch("django_q.tasks.async_task") as enqueued:
        with CaptureQueriesContext(connection) as known:
            request_reset(Client(), EMAIL, ip="198.51.100.1")
        with CaptureQueriesContext(connection) as unknown:
            request_reset(Client(), "nobody@example.invalid", ip="198.51.100.2")
    assert len(known.captured_queries) == len(unknown.captured_queries)
    assert not any("core_user" in q["sql"] for q in known.captured_queries)
    assert [call.args[0] for call in enqueued.call_args_list] == [
        "services.password_reset.send_links"
    ] * 2
    assert mail.outbox == []


def test_ip_limit_keeps_the_same_result(user: User) -> None:
    for number in range(password_reset.IP_LIMIT):
        request_reset(Client(), f"someone{number}@example.invalid", ip="192.0.2.9")
    limited = request_reset(Client(), EMAIL, ip="192.0.2.9")
    assert limited["Location"] == "/accounts/password-reset/sent/"
    assert mail.outbox == []  # 上限を超えた申請は送らない
    assert (
        AuditLog.objects.filter(
            action=AuditAction.PASSWORD_RESET_REQUEST, context={"rate_limited": True}
        ).count()
        == 1
    )
    request_reset(Client(), EMAIL, ip="192.0.2.10")  # 別の IP は数えない
    assert len(mail.outbox) == 1


def test_one_link_per_user_every_ten_minutes(user: User) -> None:
    request_reset(Client())
    request_reset(Client())
    assert len(mail.outbox) == 1
    assert AuditLog.objects.filter(
        action=AuditAction.PASSWORD_RESET_LINK, context__throttled=True
    ).exists()


def test_each_account_with_the_address_gets_its_own_link(user: User) -> None:
    make_user("tanto2", roles=("support_staff",), email=EMAIL.upper())
    request_reset(Client())
    assert sorted(m.body.count("ユーザー名: tanto2") for m in mail.outbox) == [0, 1]
    assert len({link_in(m) for m in mail.outbox}) == 2


def test_logged_in_user_is_sent_home(user: User) -> None:
    client = Client()
    client.force_login(user)
    assert client.get("/accounts/password-reset/")["Location"] == "/"


# --- リンクと再設定 ----------------------------------------------------------------------


def test_reset_with_the_link(user: User) -> None:
    request_reset(Client())
    link = link_in(mail.outbox[0])
    client = Client()
    opened = client.get(link)
    # トークンを URL から外してリダイレクトする。トークンを含む URL の応答はリファラーを送らない
    assert opened["Location"] == "/accounts/password-reset/set/"
    assert opened["Referrer-Policy"] == "no-referrer"
    # フォームのある画面は same-origin（no-referrer だとフォームの送信が Origin: null で拒否される）
    assert client.get(opened["Location"])["Referrer-Policy"] == "same-origin"
    assert client.get("/accounts/password-reset/")["Referrer-Policy"] == "same-origin"
    assert client.get("/accounts/password-reset/sent/")["Referrer-Policy"] == "no-referrer"
    device = TOTPDevice.objects.get(user=user)
    weak = client.post(
        "/accounts/password-reset/set/", {"new_password1": "short", "new_password2": "short"}
    )
    assert weak.status_code == 200 and "new_password2" in weak.context["form"].errors
    done = client.post(
        "/accounts/password-reset/set/",
        {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD},
    )
    assert done["Location"] == "/accounts/password-reset/complete/"
    user.refresh_from_db()
    assert user.check_password(NEW_PASSWORD) and not user.check_password(TEST_PASSWORD)
    assert TOTPDevice.objects.get(user=user) == device  # 二要素認証の端末は変えない
    reset_log = AuditLog.objects.get(action=AuditAction.PASSWORD_RESET)
    assert (reset_log.object_id, reset_log.actor) == (str(user.pk), user)
    assert reset_log.changes["password"] == ["***", "***"]
    # 本人に変更の通知
    notice = mail.outbox[-1]
    assert notice.to == [EMAIL] and "パスワードが変更されました" in notice.subject
    assert "心当たりがない場合は、すぐに管理者に連絡してください" in str(notice.body)
    complete = client.get(done["Location"])
    assert complete["Referrer-Policy"] == "no-referrer" and 'id="reset-locked"' not in (
        complete.content.decode()
    )
    # 同じリンクは二度と使えない
    again = Client()
    again.get(link)
    assert again.get("/accounts/password-reset/set/").status_code == 400


def test_expired_and_replaced_links_cannot_be_used(user: User) -> None:
    request_reset(Client())
    first = link_in(mail.outbox[0])
    PasswordReset.objects.update(created_at=timezone.now() - timedelta(minutes=11))
    request_reset(Client())
    second = link_in(mail.outbox[1])
    client = Client()
    client.get(first)
    assert client.get("/accounts/password-reset/set/").status_code == 400  # 新しい申請で無効
    latest = PasswordReset.objects.order_by("-pk").first()
    assert latest is not None
    PasswordReset.objects.filter(pk=latest.pk).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    client.get(second)
    assert client.get("/accounts/password-reset/set/").status_code == 400  # 期限切れ


def test_locked_account_is_told_when_it_can_sign_in(user: User) -> None:
    for _ in range(5):
        Client().post("/accounts/login/", {"username": "tanto", "password": "wrong"})
    assert "tanto" in users.locked_usernames()
    request_reset(Client())
    client = Client()
    set_password(client, link_in(mail.outbox[0]))
    html = client.get("/accounts/password-reset/complete/").content.decode()
    assert (
        'id="reset-locked"' in html
        and "分後にログインするか、管理者に解除を依頼してください" in html
    )
    assert "tanto" in users.locked_usernames()  # 再設定ではロックを解除しない
