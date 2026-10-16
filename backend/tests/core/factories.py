"""テスト用のモデル生成ヘルパ。"""

import time
from datetime import date
from typing import Any

from django.test import Client
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.oath import TOTP
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.core.models import EmploymentType, Office, Organization, Role, Staff, User, UserRole

TEST_PASSWORD = "correct-horse-battery-staple"


def make_organization(**overrides: Any) -> Organization:
    values: dict[str, Any] = {
        "name": "テスト登録支援機関",
        "registration_number": "19登-000000",
        "registration_expires_on": date(2029, 3, 31),
        "postal_code": "100-0001",
        "address": "東京都千代田区千代田1-1",
    }
    return Organization.objects.create(**(values | overrides))


def make_office(**overrides: Any) -> Office:
    values: dict[str, Any] = {
        "name": "本社",
        "postal_code": "100-0001",
        "address": "東京都千代田区千代田1-1",
    }
    return Office.objects.create(**(values | overrides))


def make_staff(office: Office | None = None, **overrides: Any) -> Staff:
    values: dict[str, Any] = {
        "name": "山田 花子",
        "is_support_staff": True,
        "employment_type": EmploymentType.FULL_TIME,
        "office": office or make_office(),
    }
    return Staff.objects.create(**(values | overrides))


def make_user(username: str, *, roles: tuple[str, ...] = (), **overrides: Any) -> User:
    user = User.objects.create_user(username=username, password=TEST_PASSWORD, **overrides)
    for code in roles:
        UserRole.objects.create(user=user, role=Role.objects.get(code=code))
    return user


def make_totp_device(user: User) -> TOTPDevice:
    return TOTPDevice.objects.create(user=user, name="authenticator", confirmed=True)


def totp_token(device: TOTPDevice, steps_ahead: int = 0) -> str:
    """端末の鍵から正しい TOTP を作る。

    同じ時間枠のコードは再利用できないため、steps_ahead で先の枠を使う。
    """
    totp = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
    totp.time = time.time() + steps_ahead * device.step
    return f"{totp.token():0{device.digits}d}"


def verified_client(user: User) -> Client:
    """パスワード＋TOTP 検証済みのクライアント。"""
    client = Client()
    client.force_login(user)
    device = TOTPDevice.objects.filter(user=user, confirmed=True).first() or make_totp_device(user)
    session = client.session
    session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    session.save()
    return client
