"""電子届出の一括申込用 Excel ツール（xlsm）から、欄の表と検査の規則を JSON に書き出す（T50。ADR-0040）。

書き出すもの（生成前の検査 services/efile_checks.py と書き込みが使う）:
- 入力シートの名前・データの開始行・様式の行数（A 列の番号の付いた行）・届出機関の欄のセル
- 隠しシート「列項目TBL」の行（列・項目名・チェック種別・桁数・桁数チェック種別・必須・業務コード種別・
  区切り・改行）。日付は「C,E,G」のように 3 つの列（年・月・日）
- 隠しシート「業務コードTBL」の表（業務コード種別ごとの表示内容漢字と業務コード）
- 入力シートの入力規則のうちリスト（静的な範囲の値、または INDIRECT の親の列と表の名前の作り方）と、
  隠しシート「連動リスト」のテーブル（INDIRECT が引く名前 → 値）
- 個別のチェック（VBA の CheckKobetsuKoumoku・GetValidRec。下の INDIVIDUAL に VBA から読んで書いた）

標準ライブラリだけで動く。出力は手で直さない。

使い方（リポジトリの直下で）:
  python tools/forms/extract_efile_columns.py efile-3-1 2026-04-01 \\
      templates/forms/efile-3-1/2026-04-01/original.xlsm > mappings/efile-3-1/2026-04-01.columns.json
"""

import hashlib
import json
import posixpath
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "x14": "http://schemas.microsoft.com/office/spreadsheetml/2009/9/main",
    "xm": "http://schemas.microsoft.com/office/excel/2006/main",
}
M = "{%s}" % NS["m"]
HEADERS = {
    "列位置": "column",
    "列項目名": "name",
    "チェック種別": "char_type",
    "桁数": "length",
    "桁数チェック種別": "length_type",
    "必須": "required",
    "業務コード種別": "code",
    "区切り": "delimiter",
    "改行": "newline",
}
TABLE_SHEETS = ("連動リスト", "列項目TBL", "業務コードTBL")

# 個別のチェック（VBA の CheckKobetsuKoumoku と GetValidRec の「個別実装箇所」を読んで書いたもの）。
# derived: 表示名を、親の列の表示名で選ぶ業務コード種別の表でコードにする列（無ければ「業務コード無し」）。
# rules: when（すべて満たすとき）に require の列が空ならエラー。条件は {"column", "set"} か
#   {"column", "code": [...]}（列のコード。derived の列は derived のコード）。
# pairs: 2 つの列の両方に値があるか、両方空（3-3 の登録番号）。
_JIYU = "[届出の事由]"
_HOUKOKU = "[報告の事由]"


def _required(label: str, item: str) -> str:
    return f"{label}が選択された時は[{item}]は必須項目になります。"


def _support_difficulty(reason: str) -> dict:
    """第3-7号・第4-3号（支援計画の実施困難・特異事案）の個別のチェック。reason は事由の欄の名前。"""
    return {
        "derived": {
            "C": {
                "parent": "B",
                "by_parent": {"支援の実施困難": "A170", "その他の特異事案": "A172"},
            },
            "L": {
                "parent": "B",
                "by_parent": {"支援の実施困難": "A171", "その他の特異事案": "A173"},
            },
        },
        "rules": [
            {
                "when": [{"column": "B", "set": True}],
                "require": "C",
                "message": _required(reason, "事由の区分"),
            },
            {
                "when": [
                    {"column": "B", "code": ["02"]},
                    {"column": "C", "code": ["05"]},
                ],
                "require": "D",
                "message": f"{reason}で「その他の特異事案」、[事由の区分]で「その他」が選択された時は"
                "[事由の区分 その他内容]は必須項目になります。",
            },
            {
                "when": [{"column": "B", "set": True}],
                "require": "E,G,I",
                "message": _required(reason, f"{reason[1:-1]}発生年月日"),
            },
            {
                "when": [{"column": "B", "set": True}],
                "require": "K",
                "message": _required(reason, "事案の概要"),
            },
            {
                "when": [{"column": "B", "set": True}],
                "require": "L",
                "message": _required(reason, "措置内容"),
            },
            {
                "when": [
                    {"column": "B", "code": ["01"]},
                    {"column": "L", "code": ["02", "03"]},
                ],
                "require": "M",
                "message": f"{reason}で「支援の実施困難」、[措置内容]で「実施再開困難」「実施再開不可能」が"
                "選択された時は[措置内容　理由]は必須項目になります。",
            },
            {
                "when": [
                    {"column": "B", "code": ["02"]},
                    {"column": "L", "code": ["02", "03"]},
                ],
                "require": "M",
                "message": f"{reason}で「その他の特異事案」、[措置内容]で「問題解決困難」「問題解決不可能」が"
                "選択された時は[措置内容　理由]は必須項目になります。",
            },
        ],
        "pairs": [],
    }


INDIVIDUAL: dict[str, dict] = {
    "efile-3-1": {
        "derived": {},
        "rules": [
            {
                "when": [{"column": "B", "code": ["01"]}],
                "require": column,
                "message": _required(f"{_JIYU}で「A.特定技能雇用契約の変更 」", item),
            }
            for column, item in (
                ("I", "変更事項 大分類"),
                ("J", "変更事項 小分類"),
                ("K", "変更内容 変更後の内容"),
            )
        ]
        + [
            {
                "when": [{"column": "B", "code": ["02"]}],
                "require": column,
                "message": _required(f"{_JIYU}で「B.特定技能雇用契約の終了」", item),
            }
            for column, item in (
                ("L", "終了の事由"),
                ("M", "終了の事由 事由の詳細"),
                ("N", "終了の事由 その他の理由"),
            )
        ]
        + [
            {
                "when": [{"column": "B", "code": ["03"]}],
                "require": "O",
                "message": _required(
                    f"{_JIYU}で「C.新たな特定技能雇用契約の締結」",
                    "新たな雇用契約の締結_契約の内容",
                ),
            }
        ],
        "pairs": [],
    },
    "efile-3-2": {"derived": {}, "rules": [], "pairs": []},
    "efile-3-3": {
        "derived": {},
        "rules": [
            {
                "when": [{"column": "B", "code": ["01"]}],
                "require": "I",
                "message": _required(
                    f"{_JIYU}で「支援委託契約の締結」", "Ａ　契約の締結 契約の内容"
                ),
            },
            {
                "when": [{"column": "B", "code": ["02"]}],
                "require": "J",
                "message": _required(
                    f"{_JIYU}で「支援委託契約の変更」", "Ｂ　変更の内容 変更事項"
                ),
            },
            {
                "when": [{"column": "B", "code": ["02"]}],
                "require": "K",
                "message": _required(
                    f"{_JIYU}で「支援委託契約の変更」", "Ｂ　変更の内容 変更後の内容"
                ),
            },
            {
                "when": [{"column": "B", "code": ["03"]}],
                "require": "L",
                "message": _required(
                    f"{_JIYU}で「支援委託契約の終了」", "終了の事由 大分類"
                ),
            },
            {
                "when": [{"column": "B", "code": ["03"]}],
                "require": "M",
                "message": _required(
                    f"{_JIYU}で「支援委託契約の終了」", "終了の事由 小分類"
                ),
            },
            {
                "when": [
                    {"column": "B", "code": ["03"]},
                    {"column": "M", "code": ["07"]},
                ],
                "require": "N",
                "message": f"{_JIYU}で「支援委託契約の終了」、[終了の事由 小分類]で「その他」が選択された時は"
                "[終了の事由 その他の理由]は必須項目になります。",
            },
        ],
        "pairs": [
            {
                "columns": ["AA", "AC"],
                "message": "[登録支援機関 登録番号]は前２桁と後６桁の両方を入力してください。",
            }
        ],
    },
    "efile-3-4": {
        "derived": {
            "C": {
                "parent": "B",
                "by_parent": {
                    "特定技能所属機関の都合": "A036",
                    "特定技能外国人の都合": "A037",
                },
            }
        },
        "rules": [
            {
                "when": [],
                "require": "C",
                "message": "必須入力項目が未入力になっています。",
            }
        ],
        "pairs": [],
    },
    "efile-3-5": {"derived": {}, "rules": [], "pairs": []},
    "efile-3-7": _support_difficulty(_JIYU),
    "efile-4-3": _support_difficulty(_HOUKOKU),
}


def _strings(z: zipfile.ZipFile) -> list[str]:
    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    texts = []
    for si in root.findall("m:si", NS):
        # ふりがな（rPh）は値に含めない
        parts = [si.find("m:t", NS)] + [
            run.find("m:t", NS) for run in si.findall("m:r", NS)
        ]
        texts.append("".join(part.text or "" for part in parts if part is not None))
    return texts


def _sheet_parts(z: zipfile.ZipFile) -> dict[str, str]:
    workbook = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    targets = {rel.get("Id"): rel.get("Target", "") for rel in rels}
    result = {}
    sheets = workbook.find("m:sheets", NS)
    for sheet in sheets if sheets is not None else []:
        target = targets[sheet.get("{%s}id" % NS["r"])].lstrip("/")
        result[sheet.get("name", "")] = (
            target if target.startswith("xl/") else "xl/" + target
        )
    return result


def _column(letters: str) -> int:
    number = 0
    for char in letters:
        number = number * 26 + ord(char) - 64
    return number


def _letters(number: int) -> str:
    text = ""
    while number:
        number, rest = divmod(number - 1, 26)
        text = chr(65 + rest) + text
    return text


def _cells(
    z: zipfile.ZipFile, part: str, strings: list[str]
) -> dict[tuple[int, int], str]:
    cells: dict[tuple[int, int], str] = {}
    for cell in ET.fromstring(z.read(part)).iter(M + "c"):
        match = re.match(r"([A-Z]+)(\d+)", cell.get("r", ""))
        if match is None:
            continue
        value = cell.find("m:v", NS)
        text = ""
        if cell.get("t") == "s" and value is not None and value.text is not None:
            text = strings[int(value.text)]
        elif cell.get("t") == "inlineStr":
            text = "".join(t.text or "" for t in cell.iter(M + "t"))
        elif value is not None and value.text is not None:
            text = value.text
        cells[(int(match.group(2)), _column(match.group(1)))] = text
    return cells


def _range(ref: str) -> tuple[int, int, int, int]:
    """「$S$3:$S$5」→ (行1, 列1, 行2, 列2)。"""
    parts = ref.replace("$", "").split(":")
    first = re.match(r"([A-Z]+)(\d+)", parts[0])
    last = re.match(r"([A-Z]+)(\d+)", parts[-1])
    assert first is not None and last is not None
    return (
        int(first.group(2)),
        _column(first.group(1)),
        int(last.group(2)),
        _column(last.group(1)),
    )


def _code_tables(cells: dict[tuple[int, int], str]) -> dict[str, list[list[str]]]:
    """業務コードTBL: 2 行目の見出し（項番・業務コード種別・表示内容漢字・業務コード）が 4 列ずつ並ぶ。"""
    tables: dict[str, list[list[str]]] = {}
    column = 1
    while cells.get((2, column)) == "項番":
        code_type = cells.get((3, column + 1), "")
        rows = []
        row = 3
        while cells.get((row, column + 2), ""):
            rows.append([cells[(row, column + 2)], cells.get((row, column + 3), "")])
            row += 1
        if code_type:
            tables[code_type] = rows
        column += 4
    return tables


def _linked_tables(z: zipfile.ZipFile, linked_part: str) -> dict[str, list[str]]:
    """連動リストのシートのテーブル（INDIRECT が引く名前 → 値）。"""
    rels_path = posixpath.join(
        posixpath.dirname(linked_part),
        "_rels",
        posixpath.basename(linked_part) + ".rels",
    )
    strings = _strings(z)
    cells = _cells(z, linked_part, strings)
    tables: dict[str, list[str]] = {}
    for rel in ET.fromstring(z.read(rels_path)):
        if not rel.get("Type", "").endswith("/table"):
            continue
        path = posixpath.normpath(
            posixpath.join(posixpath.dirname(linked_part), rel.get("Target"))
        )
        table = ET.fromstring(z.read(path))
        top, left, bottom, _right = _range(table.get("ref", ""))
        values = [cells.get((row, left), "") for row in range(top + 1, bottom + 1)]
        tables[table.get("displayName", "")] = [value for value in values if value]
    return dict(sorted(tables.items()))


def _sheet_cell_range(
    z: zipfile.ZipFile, parts: dict[str, str], strings: list[str], formula: str
) -> list[str]:
    """「業務コードTBL!$S$3:$S$5」の値（空を除く）。"""
    sheet, ref = formula.split("!")
    cells = _cells(z, parts[sheet.strip("'")], strings)
    top, left, bottom, right = _range(ref)
    values = [
        cells.get((row, column), "")
        for row in range(top, bottom + 1)
        for column in range(left, right + 1)
    ]
    return [value for value in values if value]


def _lists(
    z: zipfile.ZipFile, parts: dict[str, str], strings: list[str], input_part: str
) -> dict[str, dict]:
    """入力シートのリストの入力規則（列 → 静的な値、または INDIRECT の親）。"""
    root = ET.fromstring(z.read(input_part))
    found: list[tuple[str, str]] = []
    for validation in root.iter(M + "dataValidation"):
        if validation.get("type") == "list":
            formula = validation.find("m:formula1", NS)
            found.append(
                (
                    validation.get("sqref", ""),
                    formula.text if formula is not None else "",
                )
            )
    for validation in root.iter("{%s}dataValidation" % NS["x14"]):
        if validation.get("type") == "list":
            formula = validation.find("x14:formula1/xm:f", NS)
            sqref = validation.find("xm:sqref", NS)
            found.append(
                (
                    sqref.text if sqref is not None else "",
                    formula.text if formula is not None else "",
                )
            )
    lists: dict[str, dict] = {}
    for sqref, formula in found:
        areas = sqref.split()
        origin = re.match(r"([A-Z]+)(\d+)", areas[0])
        assert origin is not None
        origin_column = _column(origin.group(1))
        indirect = re.match(r"INDIRECT\((.*)\)$", formula)
        for area in areas:
            letters = re.match(r"([A-Z]+)", area)
            assert letters is not None
            column = letters.group(1)
            if column in lists:
                continue
            if indirect is None:
                lists[column] = {
                    "kind": "static",
                    "values": _sheet_cell_range(z, parts, strings, formula),
                }
                continue
            inner = indirect.group(1)
            reference = re.search(r"\b([A-Z]+)\d+\b", inner)
            assert reference is not None
            # 相対参照: 入力規則の範囲の左上からのずれで、各列の親の列を決める
            offset = _column(reference.group(1)) - origin_column
            suffix = re.search(r'&"([^"]*)"', inner)
            lists[column] = {
                "kind": "linked",
                "parent": _letters(_column(column) + offset),
                "substitute": "SUBSTITUTE" in inner,
                "suffix": suffix.group(1) if suffix else "",
            }
    return dict(sorted(lists.items(), key=lambda item: _column(item[0])))


def _header_cells(input_xml: str) -> dict[str, str]:
    """届出機関の欄（4〜6 行の、A 列の見出しの右の結合の左上）。"""
    merges = re.findall(r'<mergeCell ref="([A-Z]+)(\d+):([A-Z]+)(\d+)"', input_xml)
    cells = {}
    for key, row in (("corporate_number", "4"), ("name", "5"), ("address", "6")):
        starts = sorted(
            (_column(first), first)
            for first, top, _last, bottom in merges
            if top == row and bottom == row and first != "A"
        )
        cells[key] = f"{starts[0][1]}{row}"
    return cells


def extract(form_id: str, valid_from: str, path: Path) -> dict:
    z = zipfile.ZipFile(path)
    strings = _strings(z)
    parts = _sheet_parts(z)
    input_sheet = next(name for name in parts if name not in TABLE_SHEETS)
    input_part = parts[input_sheet]
    table = _cells(z, parts["列項目TBL"], strings)
    header = {column: text for (row, column), text in table.items() if row == 1}
    columns_of = {
        HEADERS[text]: column for column, text in header.items() if text in HEADERS
    }
    columns = []
    for row in sorted({row for row, _column_number in table if row > 1}):
        item = {
            key: table.get((row, index), "") or "" for key, index in columns_of.items()
        }
        if not item["column"]:
            continue
        columns.append(
            {
                "column": item["column"],
                "name": item["name"],
                "char_type": item["char_type"],
                "length": int(item["length"]) if item["length"] else None,
                "exact": item["length_type"] == "一致",
                "required": bool(item["required"]),
                "code": item["code"],
                "delimiter": item["delimiter"],
                "newline": bool(item["newline"]),
            }
        )
    cells = _cells(z, input_part, strings)
    numbered = sorted(
        (row, int(text))
        for (row, column), text in cells.items()
        if column == 1 and row > 7 and text.isdigit()
    )
    first_row = next(row for row, number in numbered if number == 1)
    rows = max(number for _row, number in numbered)
    input_xml = z.read(input_part).decode("utf-8")
    return {
        "description": "電子届出の一括申込用 Excel ツールの欄の表と検査の規則。"
        "tools/forms/extract_efile_columns.py が作る。手で直さない。",
        "form_id": form_id,
        "valid_from": valid_from,
        "tool": {
            "file": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "title": cells.get((1, 1), ""),
        },
        "sheet": input_sheet,
        "first_row": first_row,
        "rows": rows,
        "organization_label": cells.get((3, 1), ""),
        "organization_cells": _header_cells(input_xml),
        "columns": columns,
        "codes": _code_tables(_cells(z, parts["業務コードTBL"], strings)),
        "lists": _lists(z, parts, strings, input_part),
        "linked": _linked_tables(z, parts["連動リスト"]),
        "individual": INDIVIDUAL[form_id],
    }


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    form_id, valid_from, path = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    print(json.dumps(extract(form_id, valid_from, path), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
