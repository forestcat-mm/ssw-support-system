"""入管のマクロつきの Excel ツール（xlsm）の入力シートに、1 人 1 行で値を書く（T47。ADR-0039）。

ADR-0018 と同じく、ブック（zip）の中のファイルをそのまま写し、書く行の XML だけを組み立て直す。
マクロ（vbaProject.bin）・ActiveX・保護・隠しの状態・入力規則・名前定義・計算の設定は変えない。

- 文字はインライン文字列（t="inlineStr"）で書く。セルの書式（s 属性）は元のまま。
- 行の番号の数式の列（A 列）は、数式を残し、計算の結果（行の番号 − 7 ＝ 人の番号）をキャッシュとして
  書く（D2）。
- 書く行は 1 回の走査で探し、行ごとに組み立て直す（30 万セルを 1 つずつ探さない）。
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from xml.sax.saxutils import escape

from services.form_xlsx import XlsxWriteError, column_index, sheet_paths, write_cells

ROW = re.compile(r'<row r="(\d+)"(?:\s[^>]*?)?(?:/>|>.*?</row>)', re.S)
CELL = re.compile(r'<c r="([A-Z]+)(\d+)"((?:\s[^>]*?)?)(?:/>|>(.*?)</c>)', re.S)


def _text_cell(ref: str, attrs: str, value: str) -> str:
    style = re.search(r'\ss="(\d+)"', attrs)
    style_attr = f' s="{style.group(1)}"' if style else ""
    text = escape(value)
    preserve = ' xml:space="preserve"' if text != text.strip() or "\n" in text else ""
    return f'<c r="{ref}"{style_attr} t="inlineStr"><is><t{preserve}>{text}</t></is></c>'


def _formula_cell(ref: str, attrs: str, body: str, result: int) -> str:
    """数式を残し、結果（数値）だけを書く。t（文字の結果）は外す。"""
    formula = re.search(r"<f\b.*?(?:/>|</f>)", body or "", re.S)
    if formula is None:
        raise XlsxWriteError(f"{ref}: no formula to keep")
    kept = re.sub(r'\st="[^"]*"', "", attrs)
    return f'<c r="{ref}"{kept}>{formula.group(0)}<v>{result}</v></c>'


def _row_xml(
    row_xml: str,
    row_number: int,
    values: Mapping[str, str],
    formula_column: str | None,
    offset: int,
) -> str:
    """行の要素を、値を書いたものに組み立て直す（無いセルは列の順に足す）。"""
    head_end = row_xml.find(">") + 1
    if row_xml.endswith("/>"):
        head, body = row_xml[:-2] + ">", ""
    else:
        head, body = row_xml[:head_end], row_xml[head_end : row_xml.rindex("</row>")]
    cells: dict[int, str] = {}
    for match in CELL.finditer(body):
        letters, attrs, inner = match.group(1), match.group(3), match.group(4)
        ref = f"{letters}{row_number}"
        if letters == formula_column and "<f" in (inner or ""):
            result = row_number - offset  # 数式 ROW()-7 の結果（人の番号）
            cells[column_index(letters)] = _formula_cell(ref, attrs, inner or "", result)
        elif letters in values:
            cells[column_index(letters)] = _text_cell(ref, attrs, values[letters])
        else:
            cells[column_index(letters)] = match.group(0)
    for letters, value in values.items():
        index = column_index(letters)
        if index not in cells:
            cells[index] = _text_cell(f"{letters}{row_number}", "", value)
    return head + "".join(cells[index] for index in sorted(cells)) + "</row>"


def _write_sheet(
    sheet_xml: str,
    rows: Mapping[int, Mapping[str, str]],
    formula_column: str | None,
    offset: int,
) -> str:
    pieces: list[str] = []
    cursor = 0
    found: set[int] = set()
    for match in ROW.finditer(sheet_xml):
        number = int(match.group(1))
        if number not in rows:
            continue
        pieces.append(sheet_xml[cursor : match.start()])
        pieces.append(_row_xml(match.group(0), number, rows[number], formula_column, offset))
        cursor = match.end()
        found.add(number)
    missing = sorted(set(rows) - found)
    if missing:
        raise XlsxWriteError(f"rows not found in the input sheet: {missing[:5]}")
    pieces.append(sheet_xml[cursor:])
    return "".join(pieces)


def write_rows(
    source: Path,
    destination: Path,
    *,
    rows: Mapping[str, Sequence[Mapping[str, str]]],
    first_row: int,
    formula_column: str | None,
    cells: Mapping[str, object] | None = None,
) -> None:
    """source のブックを destination に写し、入力シートの行に値を書く。

    rows: シート名 → 1 人ずつの {列: 表示名}（first_row から順に書く）。空の値は書かない。
    formula_column: 行の番号の数式の列（数式のあるセルだけ結果を書く）。
    cells: 単独のセル（「シート!D15」→ 値。メニューの在留資格）。
    """
    plain = destination.with_suffix(".cells.tmp")
    if cells:
        write_cells(source, plain, cells)
        base = plain
    else:
        base = source
    try:
        with zipfile.ZipFile(base) as archive:
            paths = sheet_paths(archive)
            edited: dict[str, bytes] = {}
            for sheet, people in rows.items():
                if sheet not in paths:
                    raise XlsxWriteError(f"sheet not found: {sheet}")
                by_row = {
                    first_row + index: {col: value for col, value in person.items() if value}
                    for index, person in enumerate(people)
                }
                xml = archive.read(paths[sheet]).decode("utf-8")
                edited[paths[sheet]] = _write_sheet(
                    xml, by_row, formula_column, first_row - 1
                ).encode("utf-8")
            with zipfile.ZipFile(destination, "w") as output:
                for item in archive.infolist():
                    output.writestr(item, edited.get(item.filename, archive.read(item.filename)))
    finally:
        plain.unlink(missing_ok=True)
