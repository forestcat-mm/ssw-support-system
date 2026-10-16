"""2 ロールでログインし、管理画面での権限差を確認する。"""

import pytest
from django.test import Client

from .factories import TEST_PASSWORD, make_office, make_user

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
    assert "Office" in models
    assert "User" not in models
    assert "Role" not in models


def test_user_without_role_cannot_log_in_to_admin() -> None:
    make_user("norole")
    client = Client()
    response = client.post(
        "/admin/login/", {"username": "norole", "password": TEST_PASSWORD, "next": "/admin/"}
    )
    assert response.status_code == 200  # ログイン画面のまま
    assert client.get("/admin/").status_code == 302


def test_support_staff_logs_in_with_password() -> None:
    make_user("tanto", roles=("support_staff",))
    client = Client()
    response = client.post(
        "/admin/login/", {"username": "tanto", "password": TEST_PASSWORD, "next": "/admin/"}
    )
    assert response.status_code == 302
    assert client.get("/admin/").status_code == 200
