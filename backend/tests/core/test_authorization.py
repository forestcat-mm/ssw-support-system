import pytest
from django.contrib import admin
from django.contrib.auth.models import AnonymousUser

from apps.core.models import Action, Office, Role, RolePermission, User
from services import authorization

from .factories import make_office, make_user

pytestmark = pytest.mark.django_db


def test_user_without_role_has_no_actions() -> None:
    user = make_user("norole")
    assert authorization.actions_of(user) == frozenset()
    assert not user.has_perm("core.view_office")


def test_anonymous_and_inactive_users_have_no_actions() -> None:
    assert authorization.actions_of(AnonymousUser()) == frozenset()
    inactive = make_user("gone", roles=("admin",), is_active=False)
    assert authorization.actions_of(inactive) == frozenset()


def test_initial_role_matrix() -> None:
    admin_user = make_user("a", roles=("admin",))
    staff_user = make_user("s", roles=("support_staff",))
    assert authorization.actions_of(admin_user) == set(Action)
    assert authorization.actions_of(staff_user) == {
        Action.ADMIN_ACCESS,
        Action.ORGANIZATION_VIEW,
        Action.WORKERS_VIEW,
        Action.WORKERS_MANAGE,
        Action.HOSTS_VIEW,
        Action.HOSTS_MANAGE,
        Action.CONTRACTS_VIEW,
        Action.CONTRACTS_MANAGE,
        Action.FILINGS_VIEW,
        Action.FILINGS_MANAGE,
    }


def test_django_permissions_follow_roles() -> None:
    staff_user = make_user("s", roles=("support_staff",))
    assert staff_user.has_perm("core.view_office")
    assert not staff_user.has_perm("core.change_office")
    assert not staff_user.has_perm("core.view_user")
    # 削除は対応表にないため管理者でも拒否
    assert not make_user("a", roles=("admin",)).has_perm("core.delete_office")


def test_new_role_is_added_by_rows_only() -> None:
    """ロール追加はコード変更なしに行の追加で済む（第4章）。"""
    viewer = Role.objects.create(code="viewer", name="閲覧者")
    RolePermission.objects.create(role=viewer, action=Action.ORGANIZATION_VIEW)
    user = make_user("v", roles=("viewer",))
    assert authorization.actions_of(user) == {Action.ORGANIZATION_VIEW}


def test_scope_all_returns_everything() -> None:
    make_office(name="本社")
    make_office(name="支社")
    user = make_user("s", roles=("support_staff",))
    assert authorization.scope(user, Office.objects.all()).count() == 2
    assert authorization.scope(make_user("norole"), Office.objects.all()).count() == 0
    assert authorization.can(user, Action.ORGANIZATION_VIEW, Office.objects.first())


def test_every_admin_model_is_mapped() -> None:
    """admin に登録したモデルに権限の対応漏れがない（漏れると誰も見られない）。"""
    mapped = authorization.DJANGO_PERMISSION_ACTIONS
    missing = [
        f"{model._meta.app_label}.view_{model._meta.model_name}"
        for model in admin.site._registry
        if f"{model._meta.app_label}.view_{model._meta.model_name}" not in mapped
    ]
    assert missing == []


def test_superuser_flag_is_rejected_by_database() -> None:
    from django.db import IntegrityError, transaction

    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.create_superuser(username="root", password="x-unused-123")
