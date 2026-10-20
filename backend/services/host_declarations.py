"""受入れ機関の申告（区分V (11)〜(32)）の項目と版（T46-D5。ADR-0038）。

項目はデータ（services/data/host_declarations.json）。版は上書きせず、新しい版を足す。
"""

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from apps.hosts.models import HostDeclaration, HostOrganization

DATA = Path(__file__).resolve().parent / "data" / "host_declarations.json"
ANSWERS = ("yes", "no")


@dataclass(frozen=True)
class Item:
    key: str
    label: str
    expected: str  # 通常の答え（"yes"・"no"。空は決まった答えが無い）
    detail: bool  # 内容の欄がある
    required: bool  # ツールの必須


@cache
def items() -> tuple[Item, ...]:
    with DATA.open(encoding="utf-8") as file:
        data: dict[str, Any] = json.load(file)
    return tuple(Item(**row) for row in data["items"])


def latest(host: HostOrganization) -> HostDeclaration | None:
    """取り消していない最新の版。"""
    return host.declarations.filter(voided_at__isnull=True).first()


def unusual(declaration: HostDeclaration) -> list[Item]:
    """通常と違う答えの項目（内容を書く・確かめる）。"""
    answers = declaration.answers or {}
    return [
        item
        for item in items()
        if item.expected
        and answers.get(item.key, {}).get("answer") not in ("", None, item.expected)
    ]


def missing(declaration: HostDeclaration) -> list[Item]:
    """必須なのに答えの無い項目。"""
    answers = declaration.answers or {}
    return [
        item for item in items() if item.required and not answers.get(item.key, {}).get("answer")
    ]
