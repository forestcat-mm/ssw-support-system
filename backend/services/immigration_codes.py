"""在留申請オンラインの一括登録用ツールの選択肢の表（T46-D3。ADR-0038）。

台帳の新しい選択肢は、ツールのコードをそのまま値として持つ。表示名はツールの版のデータ
（services/data/immigration_codes.json。tools/forms/extract_tool_codes.py が作る）から出す。
"""

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

DATA = Path(__file__).resolve().parent / "data" / "immigration_codes.json"

# 台帳で使う表
RELATIONSHIP = "CB06_2"  # 在日親族の続柄（更新・変更の CB06_3 と同じ値）
COUNTRY = "CB79"  # 在日親族の国籍・受験地（日本を含む）
EVALUATION = "CC06"  # 技能水準・日本語能力の評価区分
SKILL_EXAM = "CC09"
JAPANESE_EXAM = "CC10"
INTERN_OCCUPATION = "CC02_3"  # 技能実習2号の職種（作業は職種ごとの連動の表）
INTERN_WORK = "CB30"
INTERN_PROOF = "CC07"
PERIOD_OF_STAY = "CB20"
PREFECTURE = "CA69_1"  # 都道府県（市区町村は都道府県ごとの連動の表）
INDUSTRY = "CC13"  # 所属機関の業種

# 「日本語の名前 English name」の英語の部分（最初の空白の後が ASCII の英字で始まる）
_ENGLISH = re.compile(r"[ 　]+(?=[A-Za-z(]).*$")


@cache
def _data() -> dict[str, Any]:
    with DATA.open(encoding="utf-8") as file:
        data: dict[str, Any] = json.load(file)
    return data


def version() -> str:
    return str(_data()["version"])


def table(code: str) -> list[tuple[str, str]]:
    """[(コード, 表示名)]（ツールの並び順）。"""
    return [(value, label) for label, value in _data()["codes"][code]]


def linked(code: str, parent: str) -> list[tuple[str, str]]:
    """連動の表（親の値ごとの [(コード, 表示名)]）。無ければ空。"""
    rows = _data()["linked"].get(code, {}).get(parent, [])
    return [(value, label) for label, value in rows]


def japanese(label: str) -> str:
    """表示名の日本語の部分（「中国 People's Republic of China」→「中国」）。"""
    return _ENGLISH.sub("", label).strip() or label


def choices(code: str, *, blank: bool = True) -> list[tuple[str, str]]:
    """フォームの選択肢（日本語の名前）。"""
    rows = [(value, japanese(label)) for value, label in table(code)]
    return ([("", "---------")] if blank else []) + rows


def label(code: str, value: str) -> str:
    """コードの日本語の名前（無いコードはそのまま）。"""
    if not value:
        return ""
    return next((japanese(text) for key, text in table(code) if key == value), value)


def linked_label(code: str, parent: str, value: str) -> str:
    return next((japanese(text) for key, text in linked(code, parent) if key == value), value)


def is_valid(code: str, value: str) -> bool:
    return any(key == value for key, _label in table(code))


# --- 手続ごとの表（一括申請の生成と検査。T47）--------------------------------------------------

PROCEDURES = ("coe", "extension", "change")


def procedure_table(code: str, procedure: str) -> list[tuple[str, str]]:
    """[(コード, 表示名)]。手続で違う表（by_procedure）があればそれ、無ければ共通の表。

    どちらにも無ければ空。
    """
    special = _data()["by_procedure"].get(procedure, {}).get(code)
    if special is not None:
        return [(value, label) for label, value in special["table"]]
    rows = _data()["codes"].get(code)
    return [(value, label) for label, value in rows] if rows is not None else []


def procedure_linked(code: str, parent: str, procedure: str) -> list[tuple[str, str]] | None:
    """連動の表（親の業務コード code・親の値 parent）。表が無ければ None。"""
    special = _data()["by_procedure"].get(procedure, {}).get(code)
    if special is not None and "linked" in special:
        rows = special["linked"].get(parent)
    else:
        rows = _data()["linked"].get(code, {}).get(parent)
    return None if rows is None else [(value, label) for label, value in rows]


def full_label(code: str, value: str, procedure: str) -> str:
    """ツールのセルに書く表示名（「中国 People's Republic of China」の形。無ければ空）。"""
    return next((text for key, text in procedure_table(code, procedure) if key == value), "")


def code_of(code: str, text: str, procedure: str) -> str:
    """表示名からコード（無ければ空）。"""
    return next((key for key, label in procedure_table(code, procedure) if label == text), "")
