"""法人番号（13 桁）の正規化・検査用数字の検査・ダミー生成（T12 の seed_dummy でも使う）。

先頭 1 桁が検査用数字、後ろ 12 桁が基礎番号。
検査用数字 = 9 − (Σ Pn × Qn を 9 で割った余り)
  Pn: 基礎番号の最下位の桁を 1 桁目としたときの n 桁目の数字
  Qn: n が奇数なら 1、偶数なら 2
例: 国税庁の法人番号 7000012050002
"""

import random
import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

_FULLWIDTH = str.maketrans({chr(c): chr(c - 0xFEE0) for c in range(0xFF01, 0xFF5F)})
_PATTERN = re.compile(r"^[0-9]{13}$")

# ダミーの基礎番号の先頭（実在の法人と偶然一致しにくくするため。一致しないとは保証できない）
DUMMY_PREFIX = "99"


def normalize(value: str) -> str:
    return re.sub(r"[\s\-‐]", "", value.translate(_FULLWIDTH))


def check_digit(base: str) -> int:
    """基礎番号（12 桁）から検査用数字を計算する。"""
    if not re.fullmatch(r"[0-9]{12}", base):
        raise ValueError("base must be 12 digits")
    total = sum(
        int(digit) * (1 if n % 2 == 1 else 2) for n, digit in enumerate(reversed(base), start=1)
    )
    return 9 - total % 9


def is_valid(value: str) -> bool:
    number = normalize(value)
    return bool(_PATTERN.match(number)) and int(number[0]) == check_digit(number[1:])


def validate(value: str) -> None:
    """空は許す（個人事業主は法人番号がない。必須かどうかはモデルの制約で見る）。"""
    if not value:
        return
    number = normalize(value)
    if not _PATTERN.match(number):
        raise ValidationError(
            _("Enter a 13-digit corporate number."), code="corporate_number_format"
        )
    if int(number[0]) != check_digit(number[1:]):
        raise ValidationError(
            _("The corporate number is invalid (check digit does not match)."),
            code="corporate_number_check_digit",
        )


def generate_dummy(rng: random.Random) -> str:
    """検査用数字の合ったダミーの法人番号。同じ種の rng なら同じ順に同じ値を返す。"""
    base = DUMMY_PREFIX + "".join(rng.choice("0123456789") for _ in range(10))
    return f"{check_digit(base)}{base}"
