"""通しのテスト: 管理者向けの「設定」画面（T15b）。招待・ロック解除と、支援担当者の拒否。"""

from axes.models import AccessAttempt
from django.conf import settings as django_settings
from django.core import mail
from django.utils import timezone
from playwright.sync_api import Page, expect
from pytest_django.fixtures import Settings

from apps.core.models import User
from e2e.conftest import SignIn
from tests.core.factories import make_staff, make_user


def test_admin_invites_a_user_and_unlocks_another(
    page: Page, sign_in: SignIn, administrator: User, settings: Settings
) -> None:
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    make_staff(name="新人 太郎")
    locked = make_user("locked", roles=("support_staff",))
    AccessAttempt.objects.create(
        username="locked",
        ip_address="127.0.0.1",
        user_agent="test",
        failures_since_start=django_settings.AXES_FAILURE_LIMIT,
        attempt_time=timezone.now(),
    )
    sign_in(administrator)
    page.get_by_role("navigation", name="メインメニュー").get_by_role(
        "link", name="設定"
    ).first.click()
    expect(page.get_by_role("heading", level=1)).to_have_text("設定")

    page.get_by_role("link", name="ユーザーを招待").click()
    page.get_by_label("ユーザー名").fill("shinjin")
    page.get_by_label("メールアドレス").fill("shinjin@example.invalid")
    page.get_by_role("checkbox", name="支援担当者").check()
    page.locator("#id_staff").select_option(label="新人 太郎")
    page.get_by_role("button", name="保存する").click()
    expect(page.get_by_text("shinjin@example.invalid に招待メールを送りました。")).to_be_visible()
    assert [message.to for message in mail.outbox] == [["shinjin@example.invalid"]]

    page.get_by_role("link", name="ユーザー", exact=True).click()
    row = page.locator(f'#users tr[data-user="{locked.pk}"]')
    expect(row).to_contain_text("ロック中")
    row.get_by_role("link", name="locked").click()
    page.get_by_role("link", name="ロック解除").click()
    page.get_by_role("button", name="ロックを解除する").click()
    expect(page.get_by_text("完了しました: ロック解除")).to_be_visible()
    expect(page.locator("#user")).not_to_contain_text("ロック中")


def test_support_staff_cannot_open_settings(
    page: Page, sign_in: SignIn, support_staff: User
) -> None:
    sign_in(support_staff)
    expect(page.get_by_role("link", name="設定")).to_have_count(0)
    response = page.goto(page.url.rstrip("/") + "/settings/users/")
    assert response is not None and response.status == 403
