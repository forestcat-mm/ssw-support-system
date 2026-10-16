"""ユーザーの作成・無効化・再有効化。ユーザーは削除しない（作成者・更新者・Staff・監査ログが参照する）。"""

from __future__ import annotations

from django.contrib.auth.password_validation import validate_password
from django.contrib.sessions.models import Session
from django.db import transaction
from django.utils import timezone

from apps.core.models import AuditAction, Role, User, UserRole
from services import audit
from services.invitations import revoke_pending_invitations

ADMIN_ROLE_CODE = "admin"
SUPPORT_STAFF_ROLE_CODE = "support_staff"


class LastAdminError(Exception):
    """有効な管理者が 1 人もいなくなる操作。"""


@transaction.atomic
def create_admin_user(*, username: str, email: str, password: str) -> User:
    user = User(username=username, email=email)
    validate_password(password, user)
    user.set_password(password)
    user.save()
    UserRole.objects.create(user=user, role=Role.objects.get(code=ADMIN_ROLE_CODE))
    return user


def active_admin_count(exclude: User | None = None) -> int:
    admins = User.objects.filter(is_active=True, user_roles__role__code=ADMIN_ROLE_CODE)
    if exclude is not None:
        admins = admins.exclude(pk=exclude.pk)
    return admins.distinct().count()


@transaction.atomic
def deactivate_user(user: User, *, by: User | None) -> None:
    """無効化する。Staff・作成者等の参照は残し、セッションを削除して即時ログアウトさせる。"""
    if user.user_roles.filter(role__code=ADMIN_ROLE_CODE).exists() and not active_admin_count(
        exclude=user
    ):
        raise LastAdminError(user.username)
    with audit.event(AuditAction.DEACTIVATE, target=user, actor=by) as event:
        user.is_active = False
        user.deactivated_at = timezone.now()
        user.deactivated_by = by
        user.save(update_fields=["is_active", "deactivated_at", "deactivated_by"])
        event.context["deleted_sessions"] = delete_sessions_of(user)
        event.context["revoked_invitations"] = revoke_pending_invitations(user)


@transaction.atomic
def reactivate_user(user: User, *, by: User | None) -> None:
    with audit.event(AuditAction.REACTIVATE, target=user, actor=by):
        user.is_active = True
        user.deactivated_at = None
        user.deactivated_by = None
        user.save(update_fields=["is_active", "deactivated_at", "deactivated_by"])


def delete_sessions_of(user: User) -> int:
    """ユーザーのセッションをすべて削除する（DB セッション前提。規模的に全件走査で足りる）。"""
    deleted = 0
    for session in Session.objects.filter(expire_date__gt=timezone.now()):
        if session.get_decoded().get("_auth_user_id") == str(user.pk):
            session.delete()
            deleted += 1
    return deleted
