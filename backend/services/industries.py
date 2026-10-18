"""事業所の日本標準産業分類のコードと、分野の誓約書の産業の一覧の照合（T37-D4・D5、ADR-0026）。

一覧は services/data/industries.json（tools/forms/make_industry_lists.py が様式の本文から作る）。
- 一致: 事業所のコードが一覧の項目のコード（中分類 2 桁・小分類 3 桁・細分類 4 桁）で始まり、
  除外のコードで始まらない。
- 限定つきの項目（「…に限る」）に一致したとき、または一部だけを除くコード（「細分類2922…のうち
  …以外の産業を除く」）で始まるときは、システムでは確かめられないので警告。
- 事業所のコードが項目より粗い（例: 項目は細分類なのに 3 桁しかない）ときも警告。
- どれにも一致しなければ不一致（生成不可）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

DATA_FILE = Path(__file__).resolve().parent / "data" / "industries.json"


@cache
def lists() -> dict[str, list[dict[str, Any]]]:
    data: dict[str, list[dict[str, Any]]] = json.loads(DATA_FILE.read_text("utf-8"))["lists"]
    return data


@dataclass(frozen=True)
class Match:
    result: str  # "match" / "limited" / "coarse" / "none"
    item: dict[str, Any] | None = None


def match(form_id: str, code: str) -> Match:
    code = code.strip()
    coarse: dict[str, Any] | None = None
    for item in lists()[form_id]:
        if code.startswith(item["code"]):
            if any(code.startswith(excluded) for excluded in item["excludes"]):
                continue
            partly = any(code.startswith(limited) for limited in item["limited_codes"])
            return Match("limited" if item["limited"] or partly else "match", item)
        if item["code"].startswith(code) and code:
            coarse = coarse or item
    if coarse is not None:
        return Match("coarse", coarse)
    return Match("none")
