"""監査ログの管理画面（読み取り専用・管理者のみ）と添付のダウンロード。"""

from datetime import date, timedelta
from typing import Any

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.files.base import ContentFile
from django.test import Client
from django.utils import timezone

from apps.core.models import Attachment, AttachmentKind, AuditAction, AuditLog, Office, Staff, User
from services.attachments import create_attachment
from tests.conftest import PDF_BYTES

from .factories import make_organization, make_user, verified_client

pytestmark = pytest.mark.django_db

URL = "/admin/core/auditlog/"


def _entry(
    action: AuditAction, *, actor: User | None = None, days_ago: int = 0, **kw: object
) -> AuditLog:
    entry = AuditLog(
        action=action,
        actor=actor,
        actor_username=actor.username if actor else "",
        occurred_at=timezone.now() - timedelta(days=days_ago),
        **kw,
    )
    entry.save()
    return entry


def _ids(response: Any) -> set[int]:
    return {obj.pk for obj in response.context["cl"].result_list}


def test_admin_can_list_and_filter_by_actor_target_and_period(admin_client: Client) -> None:
    alice = make_user("alice", roles=("support_staff",))
    bob = make_user("bob", roles=("support_staff",))
    office_type = ContentType.objects.get_for_model(Office)
    staff_type = ContentType.objects.get_for_model(Staff)
    a = _entry(
        AuditAction.VIEW, actor=alice, content_type=staff_type, object_id="1", object_repr="山田"
    )
    b = _entry(AuditAction.UPDATE, actor=bob, content_type=office_type, object_id="2", days_ago=40)

    assert admin_client.get(URL).status_code == 200
    assert _ids(admin_client.get(URL, {"actor__id__exact": alice.pk})) == {a.pk}
    assert _ids(admin_client.get(URL, {"content_type__id__exact": office_type.pk})) >= {b.pk}
    assert a.pk not in _ids(admin_client.get(URL, {"content_type__id__exact": office_type.pk}))

    old_day = (timezone.localdate() - timedelta(days=40)).isoformat()
    in_period = admin_client.get(
        URL, {"occurred_at__date__gte": old_day, "occurred_at__date__lte": old_day}
    )
    assert _ids(in_period) == {b.pk}
    since_yesterday = admin_client.get(
        URL, {"occurred_at__date__gte": (date.today() - timedelta(days=1)).isoformat()}
    )
    assert a.pk in _ids(since_yesterday) and b.pk not in _ids(since_yesterday)

    assert _ids(admin_client.get(URL, {"q": "山田"})) == {a.pk}


def test_audit_log_is_read_only(admin_client: Client) -> None:
    entry = _entry(AuditAction.EXPORT)
    assert admin_client.get(f"{URL}add/").status_code == 403
    detail = admin_client.get(f"{URL}{entry.pk}/change/")
    assert detail.status_code == 200
    assert detail.context["has_change_permission"] is False
    assert admin_client.post(f"{URL}{entry.pk}/change/", {"action": "view"}).status_code == 403
    assert admin_client.get(f"{URL}{entry.pk}/delete/").status_code == 403


def test_changes_are_shown_as_a_table(admin_client: Client) -> None:
    entry = _entry(AuditAction.UPDATE, changes={"address": ["旧住所", "新住所"]})
    html = admin_client.get(f"{URL}{entry.pk}/change/").content.decode()
    assert "<td>address</td><td>旧住所</td><td>新住所</td>" in html


def test_support_staff_cannot_see_audit_logs(support_staff_client: Client) -> None:
    assert support_staff_client.get(URL).status_code == 403
    app_list = support_staff_client.get("/admin/").context["app_list"]
    assert "AuditLog" not in {m["object_name"] for app in app_list for m in app["models"]}


# --- 添付のダウンロード ------------------------------------------------------------


def _certificate() -> Attachment:
    return create_attachment(
        target=make_organization(),
        kind=AttachmentKind.FOOD_COUNCIL_CERTIFICATE,
        file=ContentFile(PDF_BYTES, name="構成員証明書.pdf"),
        created_by=None,
    )


def test_download_returns_file_and_is_logged(
    support_staff_client: Client, support_staff_user: User
) -> None:
    attachment = _certificate()
    response = support_staff_client.get(f"/attachments/{attachment.pk}/download/")
    assert response.status_code == 200
    assert b"".join(response.streaming_content) == PDF_BYTES  # type: ignore[attr-defined]
    assert "attachment;" in response["Content-Disposition"]
    assert "%E6%A7%8B%E6%88%90" in response["Content-Disposition"]  # 元のファイル名（UTF-8）
    log = AuditLog.objects.get(action=AuditAction.DOWNLOAD)
    assert log.actor == support_staff_user
    assert log.object_id == str(attachment.pk)
    assert log.context["target"].startswith("core.organization:")


def test_download_requires_permission_and_mfa() -> None:
    attachment = _certificate()
    url = f"/attachments/{attachment.pk}/download/"
    no_role = verified_client(make_user("norole"))
    assert no_role.get(url).status_code == 403

    unverified = Client()
    unverified.force_login(make_user("tanto", roles=("support_staff",)))
    assert unverified.get(url)["Location"].startswith("/accounts/mfa/")

    assert Client().get(url)["Location"].startswith("/accounts/login/")
    assert not AuditLog.objects.filter(action=AuditAction.DOWNLOAD).exists()


def test_download_unknown_attachment_is_404(admin_client: Client) -> None:
    assert admin_client.get("/attachments/999999/download/").status_code == 404


def test_admin_links_point_to_download_view(admin_client: Client) -> None:
    attachment = _certificate()
    html = admin_client.get(f"/admin/core/attachment/{attachment.pk}/change/").content.decode()
    assert f'href="/attachments/{attachment.pk}/download/"' in html
