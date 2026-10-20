"""在留申請オンラインの一括登録用ツール（xlsm）から、選択肢の表（業務コード）を JSON に書き出す。

T46（ADR-0038）。台帳の新しい選択肢はツールのコードをそのまま値として持ち、表示名はこの JSON から出す
（T46-D3）。標準ライブラリだけで読む（zipfile と ElementTree）。

使い方（リポジトリの直下で）:
  python tools/forms/extract_tool_codes.py --version 2026-06-04 \\
      --out backend/services/data/immigration_codes.json \\
      coe=001453107.xlsm extension=001453108.xlsm change=001453109.xlsm

- 列項目TBL の行のうち、特定技能の欄（共通シート・区分V・同時申請）が使う業務コードを集める。
- 各コードの表は名前定義 `_<コード>_C`（表示名とコードの 2 列）。連動の表 `_<コード>_<親の値>_C` は
  親の値ごとに持つ（市区町村〔CA69_1 の都道府県ごと〕・作業〔CC02_3 の職種ごと〕など）。
- 3 本で同じ表は codes に 1 つ、手続で違う表（申請の意思の確認の文など）は by_procedure に手続ごとに持つ。
"""

import argparse
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
SSW_SHEETS = ("申請情報入力(区分V)", "申請情報入力(同時申請)")


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    texts = []
    for si in root.findall("m:si", NS):
        # ふりがな（rPh）の文字は入れない
        parts = [si.find("m:t", NS)] + [run.find("m:t", NS) for run in si.findall("m:r", NS)]
        texts.append("".join(part.text or "" for part in parts if part is not None))
    return texts


def _sheets(z: zipfile.ZipFile) -> tuple[ET.Element, dict[str, str]]:
    workbook = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    targets = {rel.get("Id"): rel.get("Target", "") for rel in rels}
    parts = {}
    sheets = workbook.find("m:sheets", NS)
    for sheet in sheets if sheets is not None else []:
        target = targets[sheet.get("{%s}id" % NS["r"])].lstrip("/")
        parts[sheet.get("name", "")] = target if target.startswith("xl/") else "xl/" + target
    return workbook, parts


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


def _cells(z: zipfile.ZipFile, part: str, strings: list[str]) -> dict[str, str]:
    values = {}
    for cell in ET.fromstring(z.read(part)).iter(M + "c"):
        value = cell.find("m:v", NS)
        if cell.get("t") == "s" and value is not None and value.text is not None:
            values[cell.get("r", "")] = strings[int(value.text)]
        elif cell.get("t") == "inlineStr":
            values[cell.get("r", "")] = "".join(t.text or "" for t in cell.iter(M + "t"))
        elif value is not None and value.text is not None:
            values[cell.get("r", "")] = value.text
    return values


def read_tool(path: Path) -> tuple[set[str], dict[str, list[list[str]]]]:
    """(特定技能の欄が使うコード, {名前定義の名前: [[表示名, コード], ...]})。"""
    z = zipfile.ZipFile(path)
    strings = _shared_strings(z)
    workbook, parts = _sheets(z)
    cache: dict[str, dict[str, str]] = {}

    def sheet_cells(name: str) -> dict[str, str]:
        if name not in cache:
            cache[name] = _cells(z, parts[name], strings)
        return cache[name]

    # 列項目TBL: A 列＝シート名、H 列＝業務コード
    table = sheet_cells("列項目TBL")
    rows: dict[int, dict[int, str]] = {}
    for ref, value in table.items():
        match = re.match(r"([A-Z]+)(\d+)", ref)
        assert match is not None
        rows.setdefault(int(match.group(2)), {})[_column(match.group(1))] = value
    common = next(
        name
        for name in parts
        if name.startswith("申請情報入力(") and "区分" not in name and name not in SSW_SHEETS
        and name != "申請情報入力(高度専門職ポイント表)"
    )
    used = {
        row[8]
        for number, row in rows.items()
        if number > 1 and row.get(1) in (common, *SSW_SHEETS) and row.get(8)
    }
    names: dict[str, list[list[str]]] = {}
    defined_names = workbook.find("m:definedNames", NS)
    for defined in defined_names if defined_names is not None else []:
        name, ref = defined.get("name", ""), defined.text or ""
        if not (name.startswith("_") and name.endswith("_C") and "!" in ref):
            continue
        sheet, cells = ref.rsplit("!", 1)
        sheet = sheet.strip("'")
        if sheet not in parts:
            continue
        first = cells.split(":")[0].replace("$", "")
        last = cells.split(":")[-1].replace("$", "")
        start = re.match(r"([A-Z]+)(\d+)", first)
        end = re.match(r"([A-Z]+)(\d+)", last)
        assert start is not None and end is not None
        label_col = start.group(1)
        code_col = _letters(_column(label_col) + 1)
        values = sheet_cells(sheet)
        pairs = []
        for row_number in range(int(start.group(2)), int(end.group(2)) + 1):
            label = values.get(f"{label_col}{row_number}")
            code = values.get(f"{code_col}{row_number}")
            if label is None and code is None:
                continue
            pairs.append([label or "", code or ""])
        names[name] = pairs
    return used, names


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", required=True, help="ツールの版（掲載ページの日付）")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("tools", nargs="+", help="手続=パス（coe=… extension=… change=…）")
    args = parser.parse_args()
    # 手続ごとの表（コード → 表、連動はコード → 親の値 → 表）
    per_tool: dict[str, dict[str, list[list[str]]]] = {}
    per_tool_linked: dict[str, dict[str, dict[str, list[list[str]]]]] = {}
    sources = []
    for argument in args.tools:
        kind, _, raw_path = argument.partition("=")
        path = Path(raw_path)
        sources.append(
            {"procedure": kind, "file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        )
        used, names = read_tool(path)
        tables = per_tool.setdefault(kind, {})
        linked = per_tool_linked.setdefault(kind, {})
        for code in sorted(used):
            main_table = names.get(f"_{code}_C")
            if main_table is None:
                continue
            tables[code] = main_table
            parents = {value for _label, value in main_table}
            prefix = f"_{code}_"
            for name, table in names.items():
                parent = name[len(prefix) : -2] if name.startswith(prefix) else ""
                if parent in parents:
                    linked.setdefault(code, {})[parent] = table
    # 手続のあいだで同じ表は codes に 1 つ、違う表は by_procedure に手続ごとに持つ
    codes: dict[str, list[list[str]]] = {}
    shared_linked: dict[str, dict[str, list[list[str]]]] = {}
    by_procedure: dict[str, dict[str, object]] = {kind: {} for kind in per_tool}
    for code in sorted({code for tables in per_tool.values() for code in tables}):
        variants = [tables[code] for tables in per_tool.values() if code in tables]
        links = [
            per_tool_linked[kind].get(code, {}) for kind, tables in per_tool.items() if code in tables
        ]
        if all(v == variants[0] for v in variants) and all(link == links[0] for link in links):
            codes[code] = variants[0]
            if links[0]:
                shared_linked[code] = dict(sorted(links[0].items()))
            continue
        for kind, tables in per_tool.items():
            if code in tables:
                entry: dict[str, object] = {"table": tables[code]}
                if per_tool_linked[kind].get(code):
                    entry["linked"] = dict(sorted(per_tool_linked[kind][code].items()))
                by_procedure[kind][code] = entry
    data = {
        "description": "在留申請オンラインの一括登録用ツールの選択肢の表（表示名とコード）。"
        "tools/forms/extract_tool_codes.py が作る。手で直さない。",
        "version": args.version,
        "sources": sources,
        "codes": codes,
        "linked": shared_linked,
        "by_procedure": by_procedure,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    differing = {kind: sorted(entries) for kind, entries in by_procedure.items()}
    print(f"{args.out}: {len(codes)} shared codes; differing by procedure: {differing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
