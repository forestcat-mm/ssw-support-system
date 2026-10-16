"""法人番号・業務区分・加入状況・契約の業務ルール。"""

import random
from datetime import date

import pytest
from django.core.exceptions import ValidationError

from apps.hosts.models import HostMembership, SupportContract
from services import categories, corporate_number, hosts

from .factories import VALID_NUMBERS, category, field, make_host

# --- 法人番号 --------------------------------------------------------------------


def test_check_digit_of_known_number() -> None:
    assert corporate_number.check_digit("000012050002") == 7  # 国税庁 7000012050002
    assert corporate_number.is_valid("7000012050002")


@pytest.mark.parametrize(
    "raw", ["7000012050003", "8000012050002", "700001205000", "70000120500021", "ABCDEFGHIJKLM"]
)
def test_invalid_corporate_numbers(raw: str) -> None:
    assert not corporate_number.is_valid(raw)
    with pytest.raises(ValidationError):
        corporate_number.validate(raw)


def test_normalize_corporate_number() -> None:
    assert (
        corporate_number.normalize("７０００ー０１２０５０００２".replace("ー", "-"))
        == "7000012050002"
    )
    assert corporate_number.normalize("7000 0120 50002") == "7000012050002"
    corporate_number.validate("")  # 個人事業主は空


def test_generate_dummy_is_reproducible_and_valid() -> None:
    assert [corporate_number.generate_dummy(random.Random(3)) for _ in range(2)] == [
        corporate_number.generate_dummy(random.Random(3)) for _ in range(2)
    ]
    rng = random.Random(11)
    for _ in range(200):
        number = corporate_number.generate_dummy(rng)
        assert corporate_number.is_valid(number)
        assert number[1:3] == corporate_number.DUMMY_PREFIX
    assert all(corporate_number.is_valid(n) for n in VALID_NUMBERS)


# --- 業務区分 --------------------------------------------------------------------

pytestmark_db = pytest.mark.django_db


@pytest.mark.django_db
def test_industrial_field_has_17_categories() -> None:
    names = list(
        categories.effective_categories(
            field("industrial_products"), date(2026, 10, 16)
        ).values_list("name", flat=True)
    )
    assert len(names) == 17
    assert names[0] == "機械金属加工" and names[-1] == "かばん製造"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 10, 16), ["飲食料品製造業全般"]),
        (date(2027, 3, 31), ["飲食料品製造業全般"]),
        (date(2027, 4, 1), ["飲食料品製造業", "水産加工業"]),
    ],
)
def test_food_categories_switch_on_2027_04_01(day: date, expected: list[str]) -> None:
    effective = categories.effective_categories(field("food_manufacturing"), day)
    assert list(effective.values_list("name", flat=True)) == expected


@pytest.mark.django_db
def test_site_categories_must_belong_to_site_field() -> None:
    hosts.validate_site_categories(field("industrial_products"), [category("sewing")])
    with pytest.raises(ValidationError, match="水産加工業"):
        hosts.validate_site_categories(
            field("industrial_products"), [category("fishery_processing")]
        )


# --- 加入状況・契約 ----------------------------------------------------------------


@pytest.mark.django_db
def test_membership_on_date() -> None:
    host = make_host()
    food = field("food_manufacturing")
    assert not hosts.is_member(host, food, date(2026, 1, 1))
    HostMembership.objects.create(
        host_organization=host,
        field=food,
        joined_on=date(2025, 4, 1),
        valid_until=date(2026, 3, 31),
    )
    assert hosts.is_member(host, food, date(2025, 4, 1))
    assert not hosts.is_member(host, food, date(2025, 3, 31))
    assert not hosts.is_member(host, food, date(2026, 4, 1))
    assert not hosts.is_member(host, field("industrial_products"), date(2025, 5, 1))


@pytest.mark.django_db
def test_active_contract_count() -> None:
    a = make_host()
    b = make_host(name="テスト食品株式会社", corporate_number=VALID_NUMBERS[1])
    SupportContract.objects.create(
        host_organization=a,
        concluded_on=date(2025, 3, 1),
        starts_on=date(2025, 4, 1),
        ends_on=date(2026, 3, 31),
    )
    SupportContract.objects.create(
        host_organization=a, concluded_on=date(2026, 3, 1), starts_on=date(2026, 4, 1)
    )
    SupportContract.objects.create(
        host_organization=b, concluded_on=date(2026, 9, 1), starts_on=date(2026, 10, 1)
    )
    assert hosts.active_contract_count(date(2025, 6, 1)) == 1
    assert hosts.active_contract_count(date(2026, 10, 16)) == 2
    assert hosts.active_contract(a, date(2026, 10, 16)).starts_on == date(2026, 4, 1)  # type: ignore[union-attr]


def test_periods_overlap() -> None:
    assert hosts.periods_overlap((date(2025, 4, 1), date(2026, 3, 31)), (date(2026, 3, 31), None))
    assert not hosts.periods_overlap(
        (date(2025, 4, 1), date(2026, 3, 31)), (date(2026, 4, 1), None)
    )
    assert hosts.periods_overlap((date(2025, 4, 1), None), (date(2030, 1, 1), date(2030, 12, 31)))
