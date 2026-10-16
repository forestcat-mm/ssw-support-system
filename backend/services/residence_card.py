"""在留カード番号の正規化・形式検査・ダミー生成（T12 の seed_dummy でも使う）。

形式: 英字 2 桁 + 数字 8 桁 + 英字 2 桁（例: AB12345678CD）。
"""

import random
import re
import string

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

PATTERN = re.compile(r"^[A-Z]{2}[0-9]{8}[A-Z]{2}$")
_FULLWIDTH = str.maketrans({chr(c): chr(c - 0xFEE0) for c in range(0xFF01, 0xFF5F)})


def normalize(value: str) -> str:
    """全角→半角、大文字化、空白・ハイフンの除去。保存するのはこの形。"""
    return re.sub(r"[\s\-‐]", "", value.translate(_FULLWIDTH)).upper()


def is_valid(value: str) -> bool:
    return PATTERN.match(normalize(value)) is not None


def validate(value: str) -> None:
    """空は許す（入国前は在留カードがない）。形式違反は ValidationError。"""
    if value and not is_valid(value):
        raise ValidationError(
            _(
                "Enter a residence card number as 2 letters, 8 digits and 2 letters "
                "(e.g. AB12345678CD)."
            ),
            code="invalid_residence_card_number",
        )


def generate_dummy(rng: random.Random) -> str:
    """形式どおりのダミー番号（実在の番号を使わないため）。

    同じ種の rng なら同じ順に同じ値を返す。
    """
    letters = string.ascii_uppercase
    return (
        "".join(rng.choice(letters) for _ in range(2))
        + "".join(rng.choice(string.digits) for _ in range(8))
        + "".join(rng.choice(letters) for _ in range(2))
    )
