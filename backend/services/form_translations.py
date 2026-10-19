"""訳文の版（自作の中国語の訳文。第1-6号・第1-17号）で生成するときの値の置き換え（T43、ADR-0035）。

訳の表（services/data/translations/<言語>.json）で、欄に入れる値のうち次のものだけを置き換える
（T43-D5）。
- values: 値の全体が表の文と同じもの（標準の支援計画の実施予定の初期値・様式の言葉・
  分野と業務区分・国名）
- patterns: 値の全体が形の決まった文に合うもの（締切日・時刻・更新の上限など。正規表現）
- token_lists: 区切りで分けた語がすべて表にあるもの（休日の曜日・業務区分の並び）
ほかの日本語の値（自由記述）は日本語のまま出し、keep_keys に当たらない欄の名前を UNTRANSLATED に残す
（生成前の検査の警告。氏名・住所などは訳さないのが正しいので数えない）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent / "data" / "translations"
JAPANESE = re.compile(r"[぀-ヿ㐀-鿿]")
UNTRANSLATED = "_untranslated"  # 訳の無い日本語の値の欄（スナップショットにも残る）


@dataclass(frozen=True)
class Table:
    values: dict[str, str]
    patterns: tuple[tuple[re.Pattern[str], str], ...]
    token_lists: tuple[tuple[str, str, dict[str, str]], ...]
    keep_keys: tuple[str, ...]


@cache
def table(language: str) -> Table:
    data = json.loads((ROOT / f"{language}.json").read_text(encoding="utf-8"))
    values: dict[str, str] = data["values"]
    return Table(
        values=values,
        patterns=tuple((re.compile(item["pattern"]), item["replace"]) for item in data["patterns"]),
        token_lists=tuple(
            (
                item["separator"],
                item["join"],
                values if item["tokens"] == "values" else dict(item["tokens"]),
            )
            for item in data["token_lists"]
        ),
        keep_keys=tuple(data["keep_keys"]),
    )


def translate_value(value: str, found: Table) -> str | None:
    """値の訳（表に無ければ None）。"""
    if value in found.values:
        return found.values[value]
    for pattern, replace in found.patterns:
        if pattern.fullmatch(value):
            return pattern.sub(replace, value)
    for separator, join, tokens in found.token_lists:
        parts = value.split(separator)
        if len(parts) > 1 and all(part in tokens for part in parts):
            return join.join(tokens[part] for part in parts)
    return None


def kept(key: str, found: Table) -> bool:
    """訳さない欄（氏名・住所など。訳の無い値の数に入れない）。"""
    return any(word in key for word in found.keep_keys)


def translate(data: Mapping[str, Any], language: str) -> dict[str, Any]:
    """欄の値を訳した写し（訳の無い日本語の値の欄の名前を UNTRANSLATED に入れる）。"""
    found = table(language)
    result: dict[str, Any] = {}
    untranslated: list[str] = []
    for key, value in data.items():
        if key == UNTRANSLATED:
            continue
        if isinstance(value, str) and JAPANESE.search(value):
            translated = None if kept(key, found) else translate_value(value, found)
            if translated is None and not kept(key, found):
                untranslated.append(key)
            result[key] = value if translated is None else translated
        else:
            result[key] = value
    result[UNTRANSLATED] = sorted(untranslated)
    return result


def language_of(template: Any) -> str | None:
    """訳文の版なら言語（manifest の translation）。ほかは None。"""
    manifest = getattr(template, "manifest", None) or {}
    return str(template.language) if manifest.get("translation") else None
