from datetime import date

import pytest
from django.db import IntegrityError, transaction

from apps.hosts.models import (
    EntityType,
    HostCooperationConfirmation,
    HostMembership,
    SupportContract,
)

from .factories import VALID_NUMBERS, field, make_host, make_site

pytestmark = pytest.mark.django_db


def test_corporate_number_is_unique_but_blank_is_allowed_for_sole_proprietors() -> None:
    make_host()
    with pytest.raises(IntegrityError), transaction.atomic():
        make_host(name="別会社")
    make_host(name="個人A", entity_type=EntityType.SOLE_PROPRIETOR, corporate_number="")
    make_host(name="個人B", entity_type=EntityType.SOLE_PROPRIETOR, corporate_number="")


def test_corporation_requires_corporate_number() -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        make_host(corporate_number="")


def test_membership_is_one_per_field() -> None:
    host = make_host()
    HostMembership.objects.create(
        host_organization=host, field=field("food_manufacturing"), joined_on=date(2025, 1, 1)
    )
    HostMembership.objects.create(
        host_organization=host, field=field("industrial_products"), joined_on=date(2025, 1, 1)
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        HostMembership.objects.create(
            host_organization=host, field=field("food_manufacturing"), joined_on=date(2026, 1, 1)
        )


def test_cooperation_confirmation_is_one_row_per_submission() -> None:
    """1 行＝事業所×提出先への 1 回の提出（ADR-0015）。

    出し直しは新しい行で、同じ市区町村の 2 つ目の事業所も持てる。
    """
    host = make_host()
    head, second = make_site(host), make_site(host, name="第二工場")
    for site, municipality, day in (
        (head, "千代田区", date(2025, 4, 1)),
        (head, "港区", date(2025, 4, 1)),  # 住居地の市区町村
        (head, "千代田区", date(2026, 4, 1)),  # 記載事項の変更で出し直し
        (second, "千代田区", date(2025, 4, 1)),
    ):
        HostCooperationConfirmation.objects.create(
            host_organization=host, site=site, municipality=municipality, submitted_on=day
        )
    assert host.cooperation_confirmations.count() == 4


@pytest.mark.parametrize(
    ("starts_on", "ends_on", "overlaps"),
    [
        (date(2026, 3, 31), None, True),  # 終了日と同じ日に始まる → 重なる（両端を含む）
        (date(2025, 1, 1), date(2025, 4, 1), True),
        (date(2026, 4, 1), None, False),
        (date(2024, 1, 1), date(2025, 3, 31), False),
    ],
)
def test_support_contract_periods_cannot_overlap(
    starts_on: date, ends_on: date | None, overlaps: bool
) -> None:
    host = make_host()
    SupportContract.objects.create(
        host_organization=host,
        concluded_on=date(2025, 3, 1),
        starts_on=date(2025, 4, 1),
        ends_on=date(2026, 3, 31),
    )
    other = make_host(name="別会社", corporate_number=VALID_NUMBERS[1])
    SupportContract.objects.create(
        host_organization=other, concluded_on=date(2025, 3, 1), starts_on=date(2025, 4, 1)
    )  # 別の機関は重なってよい

    def create() -> None:
        with transaction.atomic():
            SupportContract.objects.create(
                host_organization=host,
                concluded_on=date(2025, 1, 1),
                starts_on=starts_on,
                ends_on=ends_on,
            )

    if overlaps:
        with pytest.raises(IntegrityError):
            create()
    else:
        create()


def test_open_ended_contract_overlaps_any_later_contract() -> None:
    host = make_host()
    SupportContract.objects.create(
        host_organization=host, concluded_on=date(2025, 3, 1), starts_on=date(2025, 4, 1)
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        SupportContract.objects.create(
            host_organization=host, concluded_on=date(2030, 1, 1), starts_on=date(2030, 4, 1)
        )


def test_contract_end_must_not_precede_start() -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        SupportContract.objects.create(
            host_organization=make_host(),
            concluded_on=date(2025, 1, 1),
            starts_on=date(2025, 4, 1),
            ends_on=date(2025, 3, 31),
        )
