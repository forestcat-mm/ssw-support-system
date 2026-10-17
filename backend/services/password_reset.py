"""パスワードの再設定（第8章「認証」、T25）。二要素認証の設定は変えない。

- 申請の画面（request_reset）は、アカウントの有無で処理と応答時間が変わらないようにする。
  画面では入力の形・IP ごとの回数の確認・監査ログ・ジョブの登録だけを行い、
  アカウントの検索・リンクの発行・メールの送信はジョブ（send_links）で行う。
- IP ごとに 1 時間 10 回まで。超えてもジョブを登録しないだけで、画面の結果は同じ
  （監査ログには残す）。
- 同じユーザーへのリンクは 10 分に 1 通まで。リンクは一度だけ使え、期限は 60 分。
  新しく発行すると前のリンクは無効になる。
- 再設定が終わったら、本人に「パスワードが変更されました」のメールを送る（ジョブ）。
"""

from __future__ import annotations

import hashlib
import math
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.mail import send_mail
from django.db import transaction
from django.http import HttpRequest
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from apps.core.models import AuditAction, AuditLog, PasswordReset, User
from services import audit, jobs
from services.client_ip import client_ip
from services.organization import organization_display_name

LINK_TTL = timedelta(minutes=60)
USER_INTERVAL = timedelta(minutes=10)
IP_LIMIT = 10
IP_WINDOW = timedelta(hours=1)
SESSION_KEY = "password_reset_token"  # リンクのトークンを URL から外してセッションに移す


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- 申請（画面） -------------------------------------------------------------------------


def request_reset(request: HttpRequest, email: str) -> bool:
    """申請を受け付ける（アカウントは探さない）。IP ごとの上限を超えたら False（画面は同じ）。"""
    ip = client_ip(request)
    since = timezone.now() - IP_WINDOW
    recent = AuditLog.objects.filter(
        action=AuditAction.PASSWORD_RESET_REQUEST, ip_address=ip, occurred_at__gte=since
    ).count()
    limited = recent >= IP_LIMIT
    audit.record(AuditAction.PASSWORD_RESET_REQUEST, context={"rate_limited": limited})
    if limited:
        return False
    base_url = request.build_absolute_uri("/").rstrip("/")
    jobs.enqueue("services.password_reset.send_links", email.strip(), ip, base_url)
    return True


# --- リンクの発行と送信（ジョブ） -------------------------------------------------------------


def _eligible(email: str) -> list[User]:
    """有効で、パスワードを設定済みのユーザー（無効化した人・招待の受諾前の人は除く）。"""
    users = User.objects.filter(email__iexact=email, is_active=True, deactivated_at__isnull=True)
    return [user for user in users.order_by("pk") if user.has_usable_password()]


def _invalidate_open_links(user: User, now: datetime) -> list[int]:
    """未使用のリンクを無効にし、無効にした ID を返す（呼び出し側が監査ログのイベントに入れる）。"""
    links = PasswordReset.objects.filter(
        user=user, used_at__isnull=True, invalidated_at__isnull=True
    )
    ids = list(links.values_list("pk", flat=True))
    links.update(invalidated_at=now)
    return ids


def send_links(email: str, ip: str | None, base_url: str) -> None:
    """メールアドレスのユーザーごとに再設定のリンクを送る（見つからなければ何もしない）。"""
    for user in _eligible(email):
        now = timezone.now()
        if PasswordReset.objects.filter(user=user, created_at__gte=now - USER_INTERVAL).exists():
            audit.record(
                AuditAction.PASSWORD_RESET_LINK,
                target=user,
                actor=None,
                context={"throttled": True},
                ip_address=ip,
            )
            continue
        token = secrets.token_urlsafe(32)
        with transaction.atomic():
            with audit.event(
                AuditAction.PASSWORD_RESET_LINK, target=user, actor=None, ip_address=ip
            ) as event:
                # 前の未使用のリンクを無効にする（update はシグナルを通らないので ID を残す）
                event.context["invalidated_links"] = _invalidate_open_links(user, now)
                reset = PasswordReset(
                    user=user,
                    token_hash=_hash(token),
                    expires_at=now + LINK_TTL,
                    requested_ip=ip,
                )
                reset.save()
                event.context["expires_at"] = reset.expires_at.isoformat()
        url = base_url + reverse("accounts:password_reset_open", args=[token])
        context = {
            "user": user,
            "url": url,
            "expires_at": reset.expires_at,
            "organization_name": organization_display_name(),
        }
        subject = render_to_string("accounts/email/password_reset_subject.txt", context).strip()
        body = render_to_string("accounts/email/password_reset_body.txt", context)
        send_mail(subject, body, None, [user.email])


# --- 再設定（画面） ------------------------------------------------------------------------


def find_valid(token: str) -> PasswordReset | None:
    """使えるリンク（未使用・無効化されていない・期限内・ユーザーが有効）。"""
    reset = PasswordReset.objects.select_related("user").filter(token_hash=_hash(token)).first()
    if (
        reset is None
        or reset.used_at is not None
        or reset.invalidated_at is not None
        or reset.expires_at <= timezone.now()
        or not reset.user.is_active
        or reset.user.deactivated_at is not None
    ):
        return None
    return reset


@dataclass(frozen=True)
class Completed:
    user: User
    lock_minutes: int | None  # ロック中なら解けるまでの分（切り上げ）


def lock_minutes(user: User) -> int | None:
    """そのユーザー名のログインのロックが解けるまでの分。ロックしていなければ None。"""
    from axes.models import AccessAttempt

    since = timezone.now() - settings.AXES_COOLOFF_TIME
    attempt = (
        AccessAttempt.objects.filter(
            username=user.get_username(),
            failures_since_start__gte=settings.AXES_FAILURE_LIMIT,
            attempt_time__gte=since,
        )
        .order_by("-attempt_time")
        .first()
    )
    if attempt is None:
        return None
    remaining = attempt.attempt_time + settings.AXES_COOLOFF_TIME - timezone.now()
    minutes: int = max(1, math.ceil(remaining.total_seconds() / 60))
    return minutes


def complete(token: str, password: str) -> Completed | None:
    """新しいパスワードを設定する。リンクが使えなければ None。二要素認証の端末は変えない。

    ロックは解除しない（解けるまでの分を返し、画面で案内する）。ほかの端末のセッションは、
    パスワードのハッシュが変わるので次のアクセスで切れる。
    """
    with transaction.atomic():
        reset = find_valid(token)
        if reset is None:
            return None
        reset = PasswordReset.objects.select_for_update().select_related("user").get(pk=reset.pk)
        if reset.used_at is not None or reset.invalidated_at is not None:
            return None
        user = reset.user
        validate_password(password, user)
        now = timezone.now()
        with audit.event(AuditAction.PASSWORD_RESET, target=user, actor=user) as event:
            user.set_password(password)
            user.save(update_fields=["password"])
            reset.used_at = now
            reset.save(update_fields=["used_at", "updated_at"])
            event.context["link"] = reset.pk
            event.context["invalidated_links"] = _invalidate_open_links(user, now)
        jobs.enqueue("services.password_reset.send_changed_notice", user.pk, now.isoformat())
    return Completed(user=user, lock_minutes=lock_minutes(user))


def send_changed_notice(user_id: int, changed_at: str) -> None:
    """本人に「パスワードが変更されました」を送る（ジョブ）。"""
    user = User.objects.filter(pk=user_id).first()
    if user is None or not user.email:
        return
    context = {
        "user": user,
        "changed_at": datetime.fromisoformat(changed_at),
        "organization_name": organization_display_name(),
    }
    subject = render_to_string("accounts/email/password_changed_subject.txt", context).strip()
    body = render_to_string("accounts/email/password_changed_body.txt", context)
    send_mail(subject, body, None, [user.email])
