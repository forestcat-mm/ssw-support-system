"""管理者向けの「設定」画面（T15b）: 権限・ユーザーの操作・担当者・自機関・事務所・監査ログ。"""

from datetime import date

import pytest
from axes.models import AccessAttempt
from django.conf import settings as django_settings
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone

from apps.core.models import (
    AttachmentKind,
    AuditAction,
    AuditLog,
    Office,
    Organization,
    Role,
    Staff,
    SupportLanguage,
    User,
)
from services import mfa, organization, users, workers
from tests.conftest import PDF_BYTES
from tests.core.factories import (
    make_office,
    make_organization,
    make_staff,
    make_totp_device,
    make_user,
)
from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db


def token(client: Client, url: str) -> str:
    page = client.get(url)
    assert page.status_code == 200, url
    return str(page.context["once_token"])


# --- 権限 -----------------------------------------------------------------------


def test_settings_are_admin_only(support_staff_client: Client, admin_client: Client) -> None:
    for url in (
        "/settings/",
        "/settings/users/",
        "/settings/staff/",
        "/settings/organization/",
        "/settings/offices/",
        "/settings/audit/",
    ):
        assert support_staff_client.get(url).status_code == 403, url
    assert "設定" not in [
        str(i["label"]) for i in support_staff_client.get("/workers/").context["nav_footer"]
    ]
    assert admin_client.get("/settings/")["Location"] == "/settings/users/"
    # 設定はサイドバーの下（仕様 5.1）
    assert "設定" in [str(i["label"]) for i in admin_client.get("/workers/").context["nav_footer"]]


def test_moved_items_are_hidden_from_admin_menu(admin_client: Client) -> None:
    models = {
        (app["app_label"], m["object_name"])
        for app in admin_client.get("/admin/").context["app_list"]
        for m in app["models"]
    }
    for hidden in [
        ("core", "User"),
        ("core", "Staff"),
        ("core", "AuditLog"),
        ("workers", "ForeignWorker"),
    ]:
        assert hidden not in models
    assert ("hosts", "HostOrganization") in models and ("core", "Country") in models


# --- ユーザー -----------------------------------------------------------------------


def test_invite_user_sends_email_and_links_staff(admin_client: Client) -> None:
    staff = make_staff(name="新人")
    response = admin_client.post(
        "/settings/users/invite/",
        {
            "once_token": token(admin_client, "/settings/users/invite/"),
            "username": "shinjin",
            "email": "shinjin@example.invalid",
            "last_name": "新",
            "first_name": "人",
            "roles": [Role.objects.get(code="support_staff").pk],
            "staff": staff.pk,
        },
    )
    user = User.objects.get(username="shinjin")
    assert response["Location"] == f"/settings/users/{user.pk}/"
    assert len(mail.outbox) == 1 and mail.outbox[0].to == ["shinjin@example.invalid"]
    assert not user.has_usable_password()
    staff.refresh_from_db()
    assert staff.user == user
    assert AuditLog.objects.filter(action=AuditAction.INVITE, object_id=str(user.pk)).exists()


def test_deactivate_and_reactivate_user(admin_client: Client) -> None:
    target = make_user("tanto2", roles=("support_staff",))
    url = f"/settings/users/{target.pk}/deactivate/"
    admin_client.post(url, {"once_token": token(admin_client, url)})
    target.refresh_from_db()
    assert not target.is_active
    url = f"/settings/users/{target.pk}/reactivate/"
    admin_client.post(url, {"once_token": token(admin_client, url)})
    target.refresh_from_db()
    assert target.is_active


def test_last_admin_cannot_be_deactivated(admin_client: Client, admin_user: User) -> None:
    url = f"/settings/users/{admin_user.pk}/deactivate/"
    response = admin_client.post(url, {"once_token": token(admin_client, url)}, follow=True)
    admin_user.refresh_from_db()
    assert admin_user.is_active
    assert "最後の管理者は無効化できません" in response.content.decode()


def test_reset_mfa(admin_client: Client) -> None:
    target = make_user("tanto3", roles=("support_staff",))
    make_totp_device(target)
    url = f"/settings/users/{target.pk}/reset-mfa/"
    admin_client.post(url, {"once_token": token(admin_client, url)})
    assert not mfa.has_confirmed_totp(target)
    entry = AuditLog.objects.get(action=AuditAction.MFA_RESET, object_id=str(target.pk))
    assert entry.context["via"] == "settings"


def test_locked_user_is_shown_and_unlocked(admin_client: Client) -> None:
    target = make_user("locked", roles=("support_staff",))
    AccessAttempt.objects.create(
        username="locked",
        ip_address="127.0.0.1",
        user_agent="test",
        failures_since_start=django_settings.AXES_FAILURE_LIMIT,
        attempt_time=timezone.now(),
    )
    assert "locked" in users.locked_usernames()
    html = admin_client.get("/settings/users/").content.decode()
    assert "ロック中" in html
    url = f"/settings/users/{target.pk}/unlock/"
    admin_client.post(url, {"once_token": token(admin_client, url)})
    assert "locked" not in users.locked_usernames()
    entry = AuditLog.objects.get(action=AuditAction.UNLOCK, object_id=str(target.pk))
    assert entry.context["removed_attempts"] == 1


def test_unknown_user_action_is_404(admin_client: Client, admin_user: User) -> None:
    assert admin_client.get(f"/settings/users/{admin_user.pk}/delete/").status_code == 404


# --- 担当者・事務所・自機関 -----------------------------------------------------------


def test_add_and_edit_staff(admin_client: Client) -> None:
    office = make_office()
    login = make_user("sato", roles=("support_staff",))
    language = SupportLanguage.objects.get(code="zh-hans")
    response = admin_client.post(
        "/settings/staff/new/",
        {
            "once_token": token(admin_client, "/settings/staff/new/"),
            "name": "佐藤 花子",
            "is_support_staff": "on",
            "employment_type": "full_time",
            "office": office.pk,
            "languages": [language.pk],
            "training_completed_on": "2026-04-01",
            "user": login.pk,
            "is_active": "on",
        },
    )
    assert response["Location"] == "/settings/staff/"
    staff = Staff.objects.get(name="佐藤 花子")
    assert staff.user == login and list(staff.languages.all()) == [language]
    assert staff.created_by is not None
    assert AuditLog.objects.filter(action=AuditAction.CREATE, object_id=str(staff.pk)).exists()
    # 役割が 1 つも無いと保存できない（DB の検査と同じ）
    page = admin_client.post(
        f"/settings/staff/{staff.pk}/",
        {
            "once_token": token(admin_client, f"/settings/staff/{staff.pk}/"),
            "name": "佐藤 花子",
            "employment_type": "full_time",
            "office": office.pk,
        },
    )
    assert page.status_code == 200
    staff.refresh_from_db()
    assert staff.is_support_staff


def test_add_office(admin_client: Client) -> None:
    admin_client.post(
        "/settings/offices/new/",
        {
            "once_token": token(admin_client, "/settings/offices/new/"),
            "name": "大阪事務所",
            "postal_code": "530-0001",
            "address": "大阪府大阪市北区",
            "is_active": "on",
        },
    )
    assert Office.objects.filter(name="大阪事務所").exists()


def test_edit_organization_and_replace_certificate(admin_client: Client) -> None:
    org = make_organization()
    url = "/settings/organization/edit/"
    form = admin_client.get(url).context["form"]
    data = {name: form[name].value() or "" for name in form.fields}
    data |= {"once_token": token(admin_client, url), "registration_expires_on": "2031-03-31"}
    admin_client.post(url, data)
    org.refresh_from_db()
    assert org.registration_expires_on == date(2031, 3, 31)

    url = "/settings/organization/certificate/"
    for name in ("old.pdf", "new.pdf"):
        admin_client.post(
            url,
            {
                "once_token": token(admin_client, url),
                "file": SimpleUploadedFile(name, PDF_BYTES, content_type="application/pdf"),
                "valid_until": "2027-03-31",
            },
        )
    certificates = organization.council_certificates(org)
    assert [c.original_filename for c in certificates] == ["new.pdf", "old.pdf"]  # 古いものも残る
    assert all(c.kind == AttachmentKind.FOOD_COUNCIL_CERTIFICATE for c in certificates)


def test_certificate_rejects_wrong_file(admin_client: Client) -> None:
    make_organization()
    url = "/settings/organization/certificate/"
    page = admin_client.post(
        url,
        {
            "once_token": token(admin_client, url),
            "file": SimpleUploadedFile("evil.pdf", b"not a pdf", content_type="application/pdf"),
        },
    )
    assert page.status_code == 200 and 'id="id_file-error"' in page.content.decode()
    assert not Organization.objects.get().attachments.exists()


# --- 監査ログ -----------------------------------------------------------------------


def test_audit_search_records_one_view_and_hides_audit_views_by_default(
    admin_client: Client,
) -> None:
    make_worker()  # 作成の監査ログが 1 件
    before = AuditLog.objects.filter(action=AuditAction.AUDIT_VIEW).count()
    page = admin_client.get("/settings/audit/?action=create")
    assert AuditLog.objects.filter(action=AuditAction.AUDIT_VIEW).count() == before + 1
    entry = AuditLog.objects.filter(action=AuditAction.AUDIT_VIEW).latest("pk")
    assert entry.context == {
        "conditions": {"action": "create", "page": "1"},
        "count": page.context["page"].paginator.count,
    }
    # 既定では「監査ログの閲覧」を出さない。含めると出る
    default = admin_client.get("/settings/audit/").context["page"].object_list
    assert all(log.action != AuditAction.AUDIT_VIEW for log in default)
    included = admin_client.get("/settings/audit/?include_audit_views=on").context["page"]
    assert any(log.action == AuditAction.AUDIT_VIEW for log in included.object_list)


def test_audit_entry_detail_records_one_view(admin_client: Client) -> None:
    worker = make_worker()
    log = AuditLog.objects.get(action=AuditAction.CREATE, object_id=str(worker.pk))
    before = AuditLog.objects.filter(action=AuditAction.AUDIT_VIEW).count()
    html = admin_client.get(f"/settings/audit/{log.pk}/").content.decode()
    assert "WANG FANG" in html
    assert AuditLog.objects.filter(action=AuditAction.AUDIT_VIEW).count() == before + 1
    entry = AuditLog.objects.filter(action=AuditAction.AUDIT_VIEW).latest("pk")
    assert entry.context == {"conditions": {"id": str(log.pk)}, "count": 1}


# --- 外国人の「無効化を外す」（業務画面）-------------------------------------------------


def test_worker_reactivation_on_business_screen(
    admin_client: Client, support_staff_client: Client
) -> None:
    worker = make_worker()
    workers.deactivate(worker, by=None, reason="重複")
    url = f"/workers/{worker.pk}/reactivate/"
    assert support_staff_client.get(url).status_code == 403
    assert url in admin_client.get(f"/workers/{worker.pk}/").content.decode()
    page = admin_client.post(url, {"once_token": token(admin_client, url), "reason": ""})
    assert page.status_code == 200
    admin_client.post(url, {"once_token": token(admin_client, url), "reason": "別人だった"})
    worker.refresh_from_db()
    assert worker.deactivated_at is None
    assert admin_client.get(url).status_code == 404  # 有効な人には出さない
