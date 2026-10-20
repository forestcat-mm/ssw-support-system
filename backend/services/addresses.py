"""日本の住所を、一括申請ツールの都道府県・市区町村のコードと以降に分ける（T46-D4。ADR-0038）。

ツールの市区町村の名前には郡が付いている（「石狩郡当別町」）。住所に郡が無いこともあるので、
郡を除いた名前でも照合する。最も長く一致した名前を選び、同じ長さで 2 つ以上なら分けない（曖昧）。
"""

import re
from dataclasses import dataclass
from functools import cache

from services import immigration_codes

# 郡の付いた町村（「大和郡山市」のような市の名前の「郡」は外さない）
_GUN = re.compile(r"^.+?郡(?=.+[町村]$)")


@dataclass(frozen=True)
class Split:
    prefecture_code: str
    municipality_code: str
    rest: str

    @property
    def ok(self) -> bool:
        return bool(self.prefecture_code and self.municipality_code)


EMPTY = Split("", "", "")


@cache
def _prefectures() -> list[tuple[str, str]]:
    return immigration_codes.table(immigration_codes.PREFECTURE)


def _names(name: str) -> set[str]:
    without = _GUN.sub("", name)
    return {name, without} if without else {name}


def split(address: str) -> Split:
    """住所を分ける。都道府県から始まらない・市区町村が決まらない・曖昧なら EMPTY。"""
    text = re.sub(r"\s+", "", address)
    prefecture = next(
        ((code, name) for code, name in _prefectures() if text.startswith(name)), None
    )
    if prefecture is None:
        return EMPTY
    code, name = prefecture
    after = text[len(name) :]
    matches: list[tuple[int, str, str]] = []
    for municipality_code, municipality in immigration_codes.linked(
        immigration_codes.PREFECTURE, code
    ):
        for candidate in _names(municipality):
            if after.startswith(candidate):
                matches.append((len(candidate), municipality_code, candidate))
    if not matches:
        return EMPTY
    longest = max(length for length, _code, _name in matches)
    best = {(code_, name_) for length, code_, name_ in matches if length == longest}
    if len({code_ for code_, _name in best}) != 1:
        return EMPTY
    municipality_code, matched = next(iter(best))
    return Split(code, municipality_code, after[len(matched) :])


def municipality_name(prefecture_code: str, municipality_code: str) -> str:
    return immigration_codes.linked_label(
        immigration_codes.PREFECTURE, prefecture_code, municipality_code
    )
