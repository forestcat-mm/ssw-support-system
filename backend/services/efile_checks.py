"""電子届出の一括申込用ツールのマクロの入力チェックを写した検査（T50。ADR-0040 決定 4）。

列の表（tools/forms/extract_efile_columns.py の出力。様式の欄の対応の columns_table）を使い、
VBA（CreateCSV・checkValue・GetGyoumuCd・CheckKobetsuKoumoku・GetValidRec）と同じ条件で、書く行の
セルの文字（.Text）を確かめる。マクロは動かしていない（Windows の Excel での確かめは価値検証。D4）。

マクロに無く、足した検査:
- 入力規則のリスト（静的な値・連動リスト〔INDIRECT〕）に値があるか。XML で書くと入力規則は効かない。
- 見出しの字数（「全角、２０文字以内」など）を超えた値の警告（生成は止めない。T50-D9）。

未来日は、マクロが「実行した日」と比べるので、生成の日で確かめる（画面で知らせる）。
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# VBA の定数（mdlCheck・ModuleConst）の文言
REQUIRED = "必須入力項目が未入力になっています。"
NUMBER = "半角数字のみを入力して下さい。"
ALPHANUMERIC = "半角英数字のみを入力して下さい。"
DATE = "日付を入力して下さい。"
DATE_RANGE = "日付は1900/01/01～2099/12/31で入力して下さい。"
DATE_FUTURE = "本日までの日付を入力してください。"
DIGITS = "桁以内の値を入力して下さい。"
DIGITS_EXACT = "桁の値を入力して下さい。"
NO_CODE = "入力内容に対する業務コードが存在していません。"
OTHER = "システムエラーです。"
NOT_IN_LIST = "入力規則のリストにない値です。"
NOT_IN_LINKED_LIST = "「{parent}」に合う値ではありません（入力規則のリスト）。"
LONGER_THAN_HEADING = "見出しの字数（{limit} 字）を超えています。"

WIDE = "全角文字混在"
HALF_NUMBER = "半角数字のみ"
HALF_ALPHANUMERIC = "半角英数字のみ"
DATE_NOT_FUTURE = "日付(未来日不可)"
DATE_ANY = "日付"
DATE_PATTERN = re.compile(r"^\d{4}/\d{1,2}/\d{1,2}$")


@dataclass(frozen=True)
class Finding:
    """検査の結果の 1 件。row は書く行の順（0 から）。blocking でなければ警告。"""

    row: int
    column: str
    name: str
    message: str
    value: str = ""
    blocking: bool = True


# --- セルの値 ------------------------------------------------------------------------


def joined(row: Mapping[str, str], column: str, delimiter: str) -> str:
    """列項目TBL の列（日付は「C,E,G」）の値。

    VBA と同じく、値のある後ろにだけ区切りを足してつなぐ。
    """
    value = ""
    for part in column.split(","):
        if value and delimiter:
            value += delimiter
        value += row.get(part, "") or ""
    return value


def code_of(table: Mapping[str, Any], code_type: str, name: str) -> str:
    """業務コードTBL で、表示内容漢字が name と完全に一致する業務コード（無ければ空）。"""
    for display, code in table["codes"].get(code_type, []):
        if display == name:
            return str(code)
    return ""


def _vba_date(text: str) -> datetime.date | None:
    if not DATE_PATTERN.match(text):
        return None
    year, month, day = (int(part) for part in text.split("/"))
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def check_value(
    length: int | None,
    exact: bool,
    char_type: str,
    value: str,
    required: bool,
    today: datetime.date,
) -> str:
    """VBA の checkValue（必須・文字種・日付・桁数）。エラーが無ければ空。"""
    if required and value == "":
        return REQUIRED
    message = ""
    if char_type == WIDE:
        message = ""
    elif char_type == HALF_NUMBER:
        if not re.fullmatch(r"[0-9]*", value.replace(",", "").strip(" ")):
            message = NUMBER
    elif char_type == HALF_ALPHANUMERIC:
        if not re.fullmatch(r"[0-9a-zA-Z]*", value):
            message = ALPHANUMERIC
    elif char_type in (DATE_ANY, DATE_NOT_FUTURE):
        if value != "":
            day = _vba_date(value)
            if day is None:
                message = DATE
            elif not 1900 <= day.year <= 2099:
                message = DATE_RANGE
            elif char_type == DATE_NOT_FUTURE and day > today:
                message = DATE_FUTURE
    else:
        message = OTHER
    if not required and value == "":
        return message
    counted = value.replace(",", "").strip(" ").replace("\n", "\r\n")
    if counted and length is not None:
        if exact and len(counted) != length:
            message += f"{length}{DIGITS_EXACT}"
        elif not exact and len(counted) > length:
            message += f"{length}{DIGITS}"
    return message


# --- 1 行の検査 ----------------------------------------------------------------------


def _list_name(parent: str, spec: Mapping[str, Any]) -> str:
    name = parent
    if spec.get("substitute"):
        name = name.replace("（", "_").replace("）", "_").replace("，", "_")
    return name + str(spec.get("suffix", ""))


def check_row(
    table: Mapping[str, Any],
    row: Mapping[str, str],
    index: int,
    today: datetime.date,
    advice: Mapping[str, int] | None = None,
) -> list[Finding]:
    """書く 1 行（列 → セルの文字）の検査。"""
    findings: list[Finding] = []
    codes: dict[str, str] = {}
    names = {column["column"]: column["name"] for column in table["columns"]}

    def add(column: str, message: str, value: str = "", *, blocking: bool = True) -> None:
        name = names.get(column, column)
        findings.append(Finding(index, column, name, message, value, blocking))

    for column in table["columns"]:
        letters = column["column"]
        value = joined(row, letters, column["delimiter"])
        checked = value
        if column["code"] and value:
            # 業務コード種別が複数のときも、VBA は最初の種別だけを引く
            found = code_of(table, column["code"].split(",")[0], value)
            if not found:
                add(letters, NO_CODE, value)
                continue
            codes[letters] = found
            checked = found
        message = check_value(
            column["length"],
            column["exact"],
            column["char_type"],
            checked,
            column["required"],
            today,
        )
        if message:
            add(letters, message, value)

    individual = table.get("individual") or {}
    derived_codes: dict[str, str] = {}
    for letters, spec in (individual.get("derived") or {}).items():
        name = row.get(letters, "")
        code_type = spec["by_parent"].get(row.get(spec["parent"], ""), "")
        resolved = code_of(table, code_type, name) if code_type and name else ""
        derived_codes[letters] = resolved
        if name and not resolved:
            add(letters, NO_CODE, name)

    def code_in(letters: str) -> str:
        return derived_codes[letters] if letters in derived_codes else codes.get(letters, "")

    def filled(letters: str) -> bool:
        if letters in derived_codes:
            return bool(derived_codes[letters]) or bool(row.get(letters))
        return any(row.get(part) for part in letters.split(","))

    for rule in individual.get("rules") or []:
        matched = all(
            filled(condition["column"])
            if condition.get("set")
            else code_in(condition["column"]) in condition["code"]
            for condition in rule["when"]
        )
        if matched and not filled(rule["require"]):
            add(rule["require"], rule["message"])
    for pair in individual.get("pairs") or []:
        first, second = (bool(row.get(letters)) for letters in pair["columns"])
        if first != second:
            add(pair["columns"][0], pair["message"])

    for letters, spec in table.get("lists", {}).items():
        value = row.get(letters, "")
        if not value:
            continue
        if spec["kind"] == "static":
            if value not in spec["values"]:
                add(letters, NOT_IN_LIST, value)
            continue
        parent = row.get(spec["parent"], "")
        allowed = table.get("linked", {}).get(_list_name(parent, spec), [])
        if value not in allowed:
            add(
                letters,
                NOT_IN_LINKED_LIST.format(parent=names.get(spec["parent"], spec["parent"])),
                value,
            )

    for letters, limit in (advice or {}).items():
        value = row.get(letters, "")
        if len(value) > limit:
            add(letters, LONGER_THAN_HEADING.format(limit=limit), value, blocking=False)
    return findings


def check_rows(
    table: Mapping[str, Any],
    rows: Sequence[Mapping[str, str]],
    today: datetime.date,
    advice: Mapping[str, int] | None = None,
) -> list[Finding]:
    findings: list[Finding] = []
    for index, row in enumerate(rows):
        findings.extend(check_row(table, row, index, today, advice))
    return findings
