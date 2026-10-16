import random

import pytest
from django.core.exceptions import ValidationError

from services import passport, residence_card


@pytest.mark.parametrize(
    "raw",
    [
        "AB12345678CD",
        "ab12345678cd",
        "AB 1234 5678 CD",
        "AB-12345678-CD",
        "ＡＢ１２３４５６７８ＣＤ",
    ],
)
def test_residence_card_normalize_and_validate(raw: str) -> None:
    assert residence_card.normalize(raw) == "AB12345678CD"
    residence_card.validate(raw)


@pytest.mark.parametrize(
    "raw", ["A12345678CD", "AB1234567CD", "AB12345678C", "1212345678CD", "ABCDEFGHIJKL"]
)
def test_residence_card_rejects_wrong_format(raw: str) -> None:
    assert not residence_card.is_valid(raw)
    with pytest.raises(ValidationError):
        residence_card.validate(raw)


def test_blank_residence_card_is_allowed_before_entry() -> None:
    residence_card.validate("")


def test_generate_dummy_is_reproducible_and_valid() -> None:
    first = [residence_card.generate_dummy(random.Random(42)) for _ in range(3)]
    again = [residence_card.generate_dummy(random.Random(42)) for _ in range(3)]
    assert first == again
    rng = random.Random(7)
    assert all(residence_card.is_valid(residence_card.generate_dummy(rng)) for _ in range(200))


def test_passport_number_format() -> None:
    passport.validate("e1234 5678")
    assert passport.normalize("e1234 5678") == "E12345678"
    for raw in ("E123", "E1234567890123", "E1234567!"):
        with pytest.raises(ValidationError):
            passport.validate(raw)
    rng = random.Random(1)
    for _ in range(50):
        passport.validate(passport.generate_dummy(rng))
