"""多要素認証（TOTP 必須＋回復用コード）。"""

from __future__ import annotations

import base64
from urllib.parse import quote, urlencode

import qrcode
import qrcode.image.svg
from django.db import transaction
from django_otp.models import Device
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.core.models import AuditAction, RecoveryCode, RecoveryCodeDevice, User
from services import audit, recovery_codes
from services.organization import organization_display_name
from services.users import delete_sessions_of


def has_confirmed_totp(user: User) -> bool:
    return bool(TOTPDevice.objects.filter(user=user, confirmed=True).exists())


def is_verified(user: object) -> bool:
    """このセッションで TOTP（または回復用コード）を通したか。OTPMiddleware が付ける属性を見る。"""
    return bool(getattr(user, "is_verified", lambda: False)())


def pending_totp_device(user: User) -> TOTPDevice:
    """設定途中（未確定）の TOTP 端末を返す。なければ作る。"""
    device = TOTPDevice.objects.filter(user=user, confirmed=False).first()
    if device is None:
        device = TOTPDevice.objects.create(user=user, name="authenticator", confirmed=False)
    return device


def totp_issuer() -> str:
    return organization_display_name()


def totp_secret(device: TOTPDevice) -> str:
    return base64.b32encode(device.bin_key).decode().rstrip("=")


def totp_config_url(device: TOTPDevice) -> str:
    issuer = totp_issuer()
    label = quote(f"{issuer}:{device.user.get_username()}")
    params = urlencode(
        {
            "secret": totp_secret(device),
            "issuer": issuer,
            "digits": device.digits,
            "period": device.step,
            "algorithm": "SHA1",
        }
    )
    return f"otpauth://totp/{label}?{params}"


def qr_svg(data: str) -> str:
    """QR コードを SVG 文字列で返す（外部の QR 生成サービスは使わない）。"""
    image = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
    svg: bytes = image.to_string()
    return svg.decode()


@transaction.atomic
def confirm_totp(device: TOTPDevice, token: str) -> list[str] | None:
    """設定途中の TOTP を確定し、回復用コードを発行する。トークンが違えば None。

    監査ログに「多要素認証の設定」を本人の操作として 1 件残す（コードの平文は残さない）。
    """
    if not device.verify_token(token):
        return None
    user = device.user
    with audit.event(AuditAction.MFA_ENROLL, target=user, actor=user) as event:
        event.changes["totp_devices"] = [_confirmed_totp_count(user), 1]
        event.changes["unused_recovery_codes"] = [remaining_recovery_codes(user), None]
        device.confirmed = True
        device.save(update_fields=["confirmed"])
        TOTPDevice.objects.filter(user=user, confirmed=False).delete()
        codes = _issue_recovery_codes(user)
        event.changes["unused_recovery_codes"][1] = len(codes)
    return codes


@transaction.atomic
def regenerate_recovery_codes(user: User) -> list[str]:
    """回復用コードを作り直す（本人の操作）。古いコードは無効になる。監査ログに 1 件残す。"""
    with audit.event(AuditAction.RECOVERY_CODES_REGENERATE, target=user, actor=user) as event:
        before = remaining_recovery_codes(user)
        codes = _issue_recovery_codes(user)
        event.changes["unused_recovery_codes"] = [before, len(codes)]
    return codes


def _confirmed_totp_count(user: User) -> int:
    return int(TOTPDevice.objects.filter(user=user, confirmed=True).count())


def _issue_recovery_codes(user: User) -> list[str]:
    """回復用コードを発行する。平文は戻り値でだけ返す。監査は呼び出し側のイベントで行う。"""
    RecoveryCodeDevice.objects.filter(user=user).delete()
    device = RecoveryCodeDevice.objects.create(user=user, name="recovery codes", confirmed=True)
    codes = recovery_codes.generate_codes()
    RecoveryCode.objects.bulk_create(
        RecoveryCode(device=device, code_hash=recovery_codes.hash_code(code)) for code in codes
    )
    return codes


def remaining_recovery_codes(user: User) -> int:
    return RecoveryCode.objects.filter(device__user=user, used_at__isnull=True).count()


def verify_second_factor(user: User, code: str) -> Device | None:
    """TOTP（6 桁）または回復用コードを検証し、通った端末を返す。

    django_otp.match_token は全端末に試すため throttling と相性が悪い。形式で振り分ける。
    """
    code = code.strip()
    if recovery_codes.looks_like_recovery_code(code):
        devices: list[Device] = list(RecoveryCodeDevice.objects.filter(user=user, confirmed=True))
    elif code.isdigit():
        devices = list(TOTPDevice.objects.filter(user=user, confirmed=True))
    else:
        return None
    for device in devices:
        if device.verify_token(code):
            return device
    return None


@transaction.atomic
def reset_mfa(user: User, *, by: User | None, via: str = "admin") -> None:
    """TOTP 端末と回復用コードを削除し、次回ログインで再設定させる。セッションも切る。

    必ず監査ログに残す（admin のアクションは via="admin"、管理コマンドは by=None）。
    """
    with audit.event(AuditAction.MFA_RESET, target=user, actor=by, context={"via": via}) as event:
        totp = TOTPDevice.objects.filter(user=user)
        recovery = RecoveryCodeDevice.objects.filter(user=user)
        event.changes["totp_devices"] = [totp.count(), 0]
        event.changes["unused_recovery_codes"] = [remaining_recovery_codes(user), 0]
        totp.delete()
        recovery.delete()
        event.context["deleted_sessions"] = delete_sessions_of(user)
