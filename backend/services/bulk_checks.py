"""一括登録用ツールのマクロの入力チェックを写した検査（T47-D3。ADR-0039）。

列の表（mappings/bulk-*/<版>.columns.json。ツールの列項目TBL）を使い、VBA（mdlChk・mdMain の
chkInputErrRow・chkParentInput・chkChrType・chkLength・chkRequire・chkGymCode・chkParentForChild）と
同じ条件で、1 人ずつの行を確かめる。

- 共通シートと区分V は表示されているシートとして確かめる。同時申請（更新・変更）は、再入国・資格外
  活動・就労資格証明の「有」の範囲だけを確かめ、どれも無ければ必須だけ（VBA と同じ）。
- 「チェック親列」があり、親の値が「チェック親値」に当たらない列は確かめない。頭の「在」は
  共通シートの列。
- 業務コードは、表示名がツールの表（services/data/immigration_codes.json）にあるかで確かめる。リスト
  連動の子（市区町村・作業・業務区分など）は、親の値の連動の表にあるか。
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from django.utils.translation import gettext as _

from services import immigration_codes

SIMULTANEOUS = "申請情報入力(同時申請)"
YES = "有 Yes"

# 文字種ごとの、入力してよい文字（VBA removeChr と同じ）
_SYMBOLS_FULL = (
    "　゛゜ゝゞ・ーヽヾ、。，．：；"
    "！´｀¨＾￣＿〃仝々〆〇―‐／"
    "＼～∥｜…‥‘’“”（）〔〕［"
    "］｛｝〈〉《》「」『』【】＋－"
    "±×÷＝≠＜＞≦≧∞∴♂♀°′"
    "″℃￥＄￠￡％＃＆＠§☆★○●"
    "◎◇◆□△▲▽▼※〒→←↑↓〓"
    "■？＊"
)
CHAR_CLASSES: dict[str, str] = {
    "半角カナ": "｡-ﾟ",
    "半角数字": "0-9",
    "半角英字": "A-Za-z",
    "半角英大文字": "A-Z",
    "半角記号": "\x20\x21-\x2f\x3a-\x40\x5b-\x60\x7b-\x7e｡-･ｰﾞ-ﾟ",
    "半角ピリオド": ".",
    "半角カンマ": ",",
    "半角ハイフン": "\\-",
    "ひらがな": "　ぁ-ゔ゛-ゞ・-ー",
    "全角カナ": "　ァ-ヶー",
    "全角数字": "０-９",
    "全角英字": "Ａ-Ｚａ-ｚ",
    "半角スペース": " ",
    "全角スペース": "　",
    "全角記号": _SYMBOLS_FULL,
    "漢字": "一-ꀀ",
}


@dataclass(frozen=True)
class Finding:
    """1 つの不足（行は 0 から数えた人の番号）。"""

    person: int
    sheet: str
    column: str
    name: str
    message: str


def _strip(text: str, char_type: str) -> str:
    if char_type == "すべて":
        return ""
    allowed = CHAR_CLASSES.get(char_type)
    if allowed is None:
        return text
    return re.sub(f"[{allowed}]", "", text)


def char_type_ok(text: str, char_types: Sequence[str]) -> bool:
    if not text or not char_types:
        return True
    if list(char_types) == ["日付"]:
        return date_ok(text)
    remaining = text
    for char_type in char_types:
        remaining = _strip(remaining, char_type)
        if remaining == "":
            return True
    return False


def date_ok(text: str) -> bool:
    if not re.fullmatch(r"\d{8}", text):
        return False
    try:
        datetime.date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        return False
    return True


class _Row:
    """1 人分の値（シート → 列 → 表示名）。"""

    def __init__(self, values: Mapping[str, Mapping[str, str]], common: str) -> None:
        self.values = values
        self.common = common

    def get(self, sheet: str, column: str) -> str:
        return str(self.values.get(sheet, {}).get(column, "") or "")


def _parent_matches(column: Mapping[str, Any], row: _Row) -> bool:
    """チェック親列の条件に当たるか（VBA chkParentInput。条件が無ければ True）。"""
    parent = column["check_parent_column"]
    if not parent:
        return True
    sheet = column["sheet"]
    if "在" in parent:
        sheet = row.common
        parent = parent.replace("在", "")
    values: list[str] = column["check_parent_values"]
    parents = [p.strip() for p in parent.split(",")]
    texts = [row.get(sheet, p) for p in parents]
    if len(values) > 1:
        return any(
            (text != "" and value == "") or text == value for text in texts for value in values
        )
    value = values[0]
    return any((text == "" and value == "") or text == value for text in texts)


def _code_ok(
    column: Mapping[str, Any],
    text: str,
    row: _Row,
    by_cell: Mapping[tuple[str, str], Mapping[str, Any]],
    procedure: str,
) -> bool:
    code = column["code"]
    if not text or not code:
        return True
    labels = {label for _value, label in immigration_codes.procedure_table(code, procedure)}
    rel = column["rel_column"]
    if rel and not rel[-1].isdigit():
        parent_column = by_cell.get((column["rel_sheet"], rel))
        parent_text = row.get(column["rel_sheet"], rel)
        if parent_column is not None and parent_column["code"] and parent_text:
            parent_code = immigration_codes.code_of(parent_column["code"], parent_text, procedure)
            rows = immigration_codes.procedure_linked(parent_column["code"], parent_code, procedure)
            if rows is not None:
                return text in {label for _value, label in rows} or text in labels
    return text in labels


def _check_cell(
    column: Mapping[str, Any],
    text: str,
    row: _Row,
    by_cell: Mapping[tuple[str, str], Mapping[str, Any]],
    procedure: str,
) -> list[str]:
    messages = []
    name = column["name"]
    if not char_type_ok(text, column["char_types"]):
        messages.append(_("The format of %(name)s is not correct.") % {"name": name})
    length = column["length"]
    if text and length:
        if column["exact"] and len(text) != length:
            messages.append(
                _("Enter %(name)s in %(length)s characters.") % {"name": name, "length": length}
            )
        elif not column["exact"] and len(text) > length:
            messages.append(
                _("Enter %(name)s in %(length)s characters or fewer.")
                % {"name": name, "length": length}
            )
    if column["required"] and not text and column["csv_column"]:
        messages.append(_("%(name)s is required.") % {"name": name})
    if not _code_ok(column, text, row, by_cell, procedure):
        messages.append(_("%(name)s is not in the list of the tool.") % {"name": name})
    return messages


def check_rows(
    definition: Mapping[str, Any],
    people: Sequence[Mapping[str, Mapping[str, str]]],
    procedure: str,
) -> list[Finding]:
    """1 人ずつの値（シート → 列 → 表示名）を、ツールのマクロと同じ条件で確かめる。"""
    columns: list[dict[str, Any]] = definition["columns"]
    common = definition["common_sheet"]
    hidden = set(definition.get("hidden_columns", []))
    by_cell = {(c["sheet"], c["column"]): c for c in columns}
    simultaneous_visible = procedure != "coe"
    findings: list[Finding] = []
    for person, values in enumerate(people):
        row = _Row(values, common)
        flags = {letter: row.get(SIMULTANEOUS, letter) == YES for letter in ("B", "C", "D")}
        for column in columns:
            sheet = column["sheet"]
            if sheet == SIMULTANEOUS and not simultaneous_visible:
                continue
            letters = column["column"]
            text = row.get(sheet, letters)

            def add(
                message: str,
                *,
                person: int = person,
                sheet: str = sheet,
                letters: str = letters,
                name: str = column["name"],
            ) -> None:
                findings.append(Finding(person, sheet, letters, name, message))

            # リスト連動の子に値があり、親が空（VBA chkParentForChild）
            rel = column["rel_column"]
            parent_missing = False
            if text and rel and not re.fullmatch(r"AA\d+", rel) and column["rel_sheet"]:
                if row.get(column["rel_sheet"], rel) == "":
                    parent = by_cell.get((column["rel_sheet"], rel))
                    add(
                        _("When %(child)s is entered, enter %(parent)s.")
                        % {
                            "child": column["name"],
                            "parent": parent["name"] if parent else rel,
                        }
                    )
                    parent_missing = True
            if not _parent_matches(column, row) or letters in hidden or parent_missing:
                continue
            if sheet == SIMULTANEOUS:
                number = _column_number(letters)
                if not any(flags.values()):
                    if column["required"] and not text and column["csv_column"]:
                        add(_("%(name)s is required.") % {"name": column["name"]})
                    continue
                in_range = (
                    (2 <= number <= 23)
                    or (flags["B"] and 24 <= number <= 57)
                    or (flags["C"] and 58 <= number <= 95)
                    or (flags["D"] and number >= 96)
                )
                if not in_range:
                    continue
            for message in _check_cell(column, text, row, by_cell, procedure):
                add(message)
    return findings


def _column_number(letters: str) -> int:
    number = 0
    for char in letters:
        number = number * 26 + ord(char) - 64
    return number
