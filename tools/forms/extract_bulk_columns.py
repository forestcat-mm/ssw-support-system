"""在留申請オンラインの一括登録用ツール（xlsm）から、特定技能の欄の表を JSON に書き出す（T47。ADR-0039）。

隠しシート「列項目TBL」の行（シート・列・項目名・チェック種別・桁数・桁数一致・必須・業務コード・
区切り・改行・CSV 出力列〔特定技能〕・リスト連動親・チェック親・CSV ヘッダ名）を、特定技能で使う
シート（共通シート・区分V・同時申請）について書き出す。生成前の検査（services/bulk_checks.py）は
この表を使い、マクロの入力チェック（VBA mdlChk・mdMain）と同じ条件で確かめる。
標準ライブラリだけで動く。出力は手で直さない。

使い方（リポジトリの直下で）:
  python tools/forms/extract_bulk_columns.py bulk-coe 2026-06-04 \\
      templates/forms/bulk-coe/2026-06-04/original.xlsm > mappings/bulk-coe/2026-06-04.columns.json
"""

import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}
M = "{%s}" % NS["m"]
SSW = "特定技能 Specified Skilled Worker"
SECTION_V = "申請情報入力(区分V)"
SIMULTANEOUS = "申請情報入力(同時申請)"
POINTS = "申請情報入力(高度専門職ポイント表)"
FIRST_ROW, LAST_ROW = 8, 307
# 列項目TBL の列（1 行目の見出し）
HEADERS = {
    "シート": "sheet",
    "列位置": "column",
    "列項目名": "name",
    "チェック種別": "char_types",
    "桁数": "length",
    "桁数一致": "exact",
    "必須": "required",
    "業務コード": "code",
    "区切り": "delimiter",
    "改行": "newline",
    "リスト連動親シート": "rel_sheet",
    "リスト連動親列": "rel_column",
    "チェック親列": "check_parent_column",
    "チェック親値": "check_parent_values",
    "CSVヘッダ名": "csv_header",
}


def _strings(z: zipfile.ZipFile) -> list[str]:
    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    texts = []
    for si in root.findall("m:si", NS):
        parts = [si.find("m:t", NS)] + [run.find("m:t", NS) for run in si.findall("m:r", NS)]
        texts.append("".join(part.text or "" for part in parts if part is not None))
    return texts


def _sheet_parts(z: zipfile.ZipFile) -> dict[str, tuple[str, str]]:
    """シート名 → (ブックの中のパス, 状態)。"""
    workbook = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    targets = {rel.get("Id"): rel.get("Target", "") for rel in rels}
    result = {}
    sheets = workbook.find("m:sheets", NS)
    for sheet in sheets if sheets is not None else []:
        target = targets[sheet.get("{%s}id" % NS["r"])].lstrip("/")
        path = target if target.startswith("xl/") else "xl/" + target
        result[sheet.get("name", "")] = (path, sheet.get("state", "visible"))
    return result


def _column(letters: str) -> int:
    number = 0
    for char in letters:
        number = number * 26 + ord(char) - 64
    return number


def _rows(z: zipfile.ZipFile, part: str, strings: list[str]) -> dict[int, dict[int, str]]:
    rows: dict[int, dict[int, str]] = {}
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
        rows.setdefault(int(match.group(2)), {})[_column(match.group(1))] = text
    return rows


def extract(form_id: str, valid_from: str, path: Path) -> dict:
    z = zipfile.ZipFile(path)
    strings = _strings(z)
    parts = _sheet_parts(z)
    table = _rows(z, parts["列項目TBL"][0], strings)
    header = table[1]
    columns_of = {HEADERS[text]: number for number, text in header.items() if text in HEADERS}
    ssw_csv = next(number for number, text in header.items() if text == SSW)
    common = next(
        name
        for name in parts
        if name.startswith("申請情報入力(")
        and "区分" not in name
        and name not in (SIMULTANEOUS, POINTS)
    )
    sheets = [common, SECTION_V] + ([SIMULTANEOUS] if SIMULTANEOUS in parts else [])
    columns = []
    for number in sorted(table):
        row = table[number]
        if number == 1 or row.get(columns_of["sheet"]) not in sheets:
            continue
        item = {key: row.get(index, "") or "" for key, index in columns_of.items()}
        values = item["check_parent_values"]
        columns.append(
            {
                "sheet": item["sheet"],
                "column": item["column"],
                "name": item["name"],
                "char_types": [t for t in item["char_types"].split("，") if t],
                "length": int(item["length"]) if item["length"] else None,
                "exact": bool(item["exact"]),
                "required": bool(item["required"]),
                "code": item["code"],
                "delimiter": item["delimiter"],
                "newline": bool(item["newline"]),
                "csv_column": int(row[ssw_csv]) if row.get(ssw_csv) else None,
                "rel_sheet": item["rel_sheet"],
                "rel_column": item["rel_column"],
                "check_parent_column": item["check_parent_column"],
                "check_parent_values": values.split("｜") if "｜" in values else [values],
                "csv_header": item["csv_header"],
            }
        )
    # メニューの在留資格の選択肢（表示シート制御マスタ B 列）と、特定技能の非表示の列（E 列）
    control = _rows(z, parts["表示シート制御マスタ"][0], strings)
    ssw_row = next(row for row in control.values() if row.get(2) == SSW)
    hidden = [c.strip() for c in (ssw_row.get(5) or "").split(",") if c.strip()]
    menu = _rows(z, parts["メニュー"][0], strings)
    title = menu.get(12, {}).get(2, "")
    return {
        "description": "在留申請オンラインの一括登録用ツールの、特定技能の欄の表（列項目TBL）。"
        "tools/forms/extract_bulk_columns.py が作る。手で直さない。",
        "form_id": form_id,
        "valid_from": valid_from,
        "tool": {
            "file": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "title": title,
        },
        "menu": {"sheet": "メニュー", "cell": "D15", "value": SSW},
        "common_sheet": common,
        "sheets": sheets,
        "first_row": FIRST_ROW,
        "last_row": LAST_ROW,
        "row_number_column": "A",
        "hidden_columns": hidden,
        "columns": columns,
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
