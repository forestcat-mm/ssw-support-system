"""テスト用のモデル生成ヘルパ。"""

from datetime import date
from typing import Any

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
