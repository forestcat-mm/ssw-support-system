"""ユーザーの招待。招待されたユーザーはリンクからパスワードを設定し、続けて TOTP を設定する。"""

from __future__ import annotations

import enum
import hashlib
import secrets
from collections.abc import Callable, Iterable
from datetime import timedelta

from django.contrib.auth.password_validation import validate_password
from django.core.mail import send_mail
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from apps.core.models import Invitation, Role, Staff, User, UserRole
from services.organization import organization_display_name

INVITATION_TTL = timedelta(hours=72)

# 招待トークン -> 受諾画面の絶対 URL（ビューで request から作る）
UrlBuilder = Callable[[str], str]


class InvitationStatus(enum.StrEnum):
    NONE = "none"
    PENDING = "pending"
    EXPIRED = "expired"
    ACCEPTED = "accepted"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@transaction.atomic
def invite_user(
    user: User,
    *,
    roles: Iterable[Role],
    staff: Staff | None,
    by: User | None,
    build_url: UrlBuilder,
) -> Invitation:
    """未保存の User（氏名・メール等を入力済み）を、パスワード未設定のまま作成して招待する。"""
    user.set_unusable_password()
    user.save()
    UserRole.objects.bulk_create(
        UserRole(user=user, role=role, created_by=by, updated_by=by) for role in roles
    )
    if staff is not None:
        staff.user = user
        staff.updated_by = by
        staff.save(update_fields=["user", "updated_by", "updated_at"])
    return _issue(user, by=by, build_url=build_url)


@transaction.atomic
def resend_invitation(user: User, *, by: User | None, build_url: UrlBuilder) -> Invitation:
    """未受諾の招待を取り消して、新しいリンクを送る。"""
    revoke_pending_invitations(user)
    return _issue(user, by=by, build_url=build_url)


def revoke_pending_invitations(user: User) -> int:
    return Invitation.objects.filter(
        user=user, accepted_at__isnull=True, revoked_at__isnull=True
    ).update(revoked_at=timezone.now())


def find_valid_invitation(token: str) -> Invitation | None:
    """未受諾・未取消・期限内で、ユーザーが有効な招待。"""
    return (
        Invitation.objects.select_related("user")
        .filter(
            token_hash=_hash(token),
            accepted_at__isnull=True,
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
            user__is_active=True,
        )
        .first()
    )


@transaction.atomic
def accept_invitation(invitation: Invitation, password: str) -> User:
    user = invitation.user
    validate_password(password, user)
    user.set_password(password)
    user.save(update_fields=["password"])
    invitation.accepted_at = timezone.now()
    invitation.save(update_fields=["accepted_at", "updated_at"])
    return user


def invitation_status(user: User) -> InvitationStatus:
    latest = user.invitations.filter(revoked_at__isnull=True).first()
    if latest is None:
        return InvitationStatus.NONE
    if latest.accepted_at is not None:
        return InvitationStatus.ACCEPTED
    if latest.expires_at <= timezone.now():
        return InvitationStatus.EXPIRED
    return InvitationStatus.PENDING


def _issue(user: User, *, by: User | None, build_url: UrlBuilder) -> Invitation:
    token = secrets.token_urlsafe(32)
    invitation = Invitation.objects.create(
        user=user,
        token_hash=_hash(token),
        expires_at=timezone.now() + INVITATION_TTL,
        created_by=by,
        updated_by=by,
    )
    context = {
        "user": user,
        # 日本語の宛名は「姓 名」（get_full_name は「名 姓」）
        "name": f"{user.last_name} {user.first_name}".strip() or user.username,
        "url": build_url(token),
        "expires_at": invitation.expires_at,
        "organization_name": organization_display_name(),
    }
    subject = render_to_string("accounts/email/invitation_subject.txt", context).strip()
    body = render_to_string("accounts/email/invitation_body.txt", context)
    send_mail(subject, body, None, [user.email])
    # TODO(T05): 監査ログに「ユーザー招待」（対象・実行者）を記録する
    return invitation
