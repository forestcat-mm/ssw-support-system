"""2 ロールでログインし、管理画面での権限差を確認する。"""

import pytest
from django.test import Client

from apps.core.models import User

from .factories import TEST_PASSWORD, make_office, make_totp_device, make_user, totp_token

pytestmark = pytest.mark.django_db


def test_admin_can_manage_users_and_offices(admin_client: Client) -> None:
    office = make_office()
    assert admin_client.get("/admin/core/user/").status_code == 200
    assert admin_client.get("/admin/core/role/").status_code == 200
    assert admin_client.get("/admin/core/office/add/").status_code == 200
    assert admin_client.get(f"/admin/core/office/{office.pk}/change/").status_code == 200


def test_support_staff_can_view_but_not_edit(support_staff_client: Client) -> None:
    office = make_office()
    assert support_staff_client.get("/admin/").status_code == 200
    assert support_staff_client.get("/admin/core/office/").status_code == 200
    assert support_staff_client.get("/admin/core/user/").status_code == 403
    assert support_staff_client.get("/admin/core/role/").status_code == 403
    assert support_staff_client.get("/admin/core/office/add/").status_code == 403
    # 変更画面は閲覧専用で表示され、POST は拒否される
    change_url = f"/admin/core/office/{office.pk}/change/"
    response = support_staff_client.get(change_url)
    assert response.status_code == 200
    assert response.context["has_change_permission"] is False
    post = support_staff_client.post(
        change_url, {"name": "改ざん", "postal_code": "1", "address": "x"}
    )
    assert post.status_code == 403
    office.refresh_from_db()
    assert office.name == "本社"


def test_index_lists_only_permitted_models(support_staff_client: Client) -> None:
    models = {
        m["object_name"]
        for app in support_staff_client.get("/admin/").context["app_list"]
        for m in app["models"]
    }
    assert (
        "HostOrganization" in models
    )  # 事務所は設定画面に移したので admin のメニューには出ない（T15b）
    assert "User" not in models
    assert "Role" not in models


def _sign_in(username: str) -> Client:
    """ログイン画面からパスワード → TOTP の順にログインする。"""
    user = User.objects.get(username=username)
    device = make_totp_device(user)
    client = Client()
    response = client.post("/accounts/login/", {"username": username, "password": TEST_PASSWORD})
    assert response.status_code == 302
    response = client.post("/accounts/mfa/verify/", {"code": totp_token(device), "next": "/admin/"})
    assert response.status_code == 302
    assert response["Location"] == "/admin/"
    return client


def test_two_roles_sign_in_and_see_different_permissions() -> None:
    """2 ロールで実際にログインし、権限差を確認する（T04 完了条件）。"""
    make_user("kanri", roles=("admin",))
    make_user("tanto", roles=("support_staff",))
    office = make_office()

    admin = _sign_in("kanri")
    staff = _sign_in("tanto")

    for url, admin_status, staff_status in [
        ("/admin/", 200, 200),
        ("/admin/core/office/", 200, 200),
        ("/admin/core/office/add/", 200, 403),
        ("/admin/core/user/", 200, 403),
        ("/admin/core/role/", 200, 403),
        ("/admin/axes/accessattempt/", 200, 403),
    ]:
        assert admin.get(url).status_code == admin_status, url
        assert staff.get(url).status_code == staff_status, url
    change = f"/admin/core/office/{office.pk}/change/"
    assert staff.post(change, {"name": "x", "postal_code": "1", "address": "x"}).status_code == 403


def test_user_without_role_gets_403_after_sign_in() -> None:
    make_user("norole")
    client = _sign_in("norole")
    # admin は未許可を /admin/login/ に回し、ログイン済みなら 403（ログイン画面との循環を防ぐ）
    assert client.get("/admin/", follow=True).status_code == 403
