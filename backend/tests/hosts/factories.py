"""受入れ機関のテスト用ヘルパ。"""

from typing import Any

from apps.hosts.models import HostOrganization, HostSite, OperationCategory, SpecifiedSkillField

# 検査用数字の合った番号（国税庁の法人番号と、それを元に作った別の番号）
VALID_NUMBERS = ("7000012050002", "7999900000010")


def field(code: str) -> SpecifiedSkillField:
    return SpecifiedSkillField.objects.get(code=code)


def category(code: str) -> OperationCategory:
    return OperationCategory.objects.get(code=code)


def make_host(**overrides: Any) -> HostOrganization:
    values: dict[str, Any] = {
        "name": "テスト工業株式会社",
        "corporate_number": VALID_NUMBERS[0],
        "representative_title": "代表取締役",
        "representative_name": "山田 太郎",
        "postal_code": "100-0001",
        "address": "東京都千代田区千代田1-1",
    }
    return HostOrganization.objects.create(**(values | overrides))


def make_site(
    host: HostOrganization, field_code: str = "industrial_products", **overrides: Any
) -> HostSite:
    values: dict[str, Any] = {
        "name": "本社工場",
        "postal_code": "100-0001",
        "address": "東京都千代田区千代田1-1",
        "industry_code": "2194",
        "field": field(field_code),
    }
    return HostSite.objects.create(host_organization=host, **(values | overrides))
