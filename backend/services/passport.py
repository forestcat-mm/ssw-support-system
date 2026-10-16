"""旅券番号の正規化・形式検査（国ごとに形式が違うため英数字 6〜12 桁の確認だけ行う）。"""

import random
import re
import string

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from services.residence_card import normalize as _normalize_document_number

PATTERN = re.compile(r"^[A-Z0-9]{6,12}$")


def normalize(value: str) -> str:
    return _normalize_document_number(value)


def validate(value: str) -> None:
    if not PATTERN.match(normalize(value)):
        raise ValidationError(
            _("Enter a passport number of 6 to 12 letters and digits."),
            code="invalid_passport_number",
        )


def generate_dummy(rng: random.Random) -> str:
    """ダミーの旅券番号（英字 1 桁 + 数字 8 桁。中国の一般旅券に近い形）。"""
    return rng.choice(string.ascii_uppercase) + "".join(rng.choice(string.digits) for _ in range(8))
