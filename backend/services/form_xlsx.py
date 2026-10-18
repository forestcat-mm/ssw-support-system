"""公式の Excel の様式に値を書く（制約3。T29、ADR-0018）。

openpyxl で開いて保存すると、第1-17号では図形（drawing）・プリンタの設定・customXml・calcChain・
共有文字列のファイルが落ちる（ADR-0018 で確かめた）。そのため、ブック（zip）の中のファイルを
そのまま写し、指定したシートの XML の、指定したセルの要素だけを書き換える。

- 文字はインライン文字列（t="inlineStr"）で書き、共有文字列（sharedStrings.xml）は変えない。
- 数値は <v> に書く。セルの書式（s 属性）は元のまま残す。
- 計算式のセル（<f> を持つ）には書かない（エラー）。calcChain と計算の設定（calcPr）には触れない。
- 結合セルは左上のセルにだけ書く（左上以外はエラー）。
- 行・セルの要素が無ければ、列の順に挿入する（行の高さなどの属性は持たない新しい要素）。
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from collections.abc import Mapping
from pathlib import Path
from xml.sax.saxutils import escape

CELL_REF = re.compile(r"^([A-Z]{1,3})(\d+)$")
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


class XlsxWriteError(Exception):
    """書き込めないセル（計算式・結合セルの左上以外・存在しないシート）。"""


def column_index(letters: str) -> int:
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index


def split_ref(ref: str) -> tuple[str, int]:
    match = CELL_REF.match(ref)
    if match is None:
        raise XlsxWriteError(f"invalid cell reference: {ref}")
    return match.group(1), int(match.group(2))


def split_target(target: str) -> tuple[str, str]:
    """「シート名!A1」をシート名とセルに分ける。"""
    if "!" not in target:
        raise XlsxWriteError(f"target needs a sheet name: {target}")
    sheet, ref = target.rsplit("!", 1)
    return sheet.strip("'"), ref


def sheet_paths(archive: zipfile.ZipFile) -> dict[str, str]:
    """シート名 → ブックの中の XML のパス。"""
    workbook = archive.read("xl/workbook.xml").decode("utf-8")
    rels = archive.read("xl/_rels/workbook.xml.rels").decode("utf-8")
    targets = {
        match.group(1): match.group(2)
        for match in re.finditer(r'<Relationship [^>]*Id="([^"]+)"[^>]*Target="([^"]+)"', rels)
    }
    targets |= {
        match.group(2): match.group(1)
        for match in re.finditer(r'<Relationship [^>]*Target="([^"]+)"[^>]*Id="([^"]+)"', rels)
    }
    result = {}
    for match in re.finditer(r"<sheet [^>]*/>", workbook):
        tag = match.group(0)
        name = re.search(r'name="([^"]+)"', tag)
        rid = re.search(r'r:id="([^"]+)"', tag)
        if name is None or rid is None:
            continue
        target = targets[rid.group(1)]
        path = target.lstrip("/") if target.startswith("/") else posixpath.join("xl", target)
        result[_unescape(name.group(1))] = posixpath.normpath(path)
    return result


def _unescape(text: str) -> str:
    return (
        text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&amp;", "&")
    )


def merged_ranges(sheet_xml: str) -> list[tuple[int, int, int, int]]:
    """結合セルの範囲（列の始め・行の始め・列の終わり・行の終わり）。"""
    ranges = []
    for match in re.finditer(r'<mergeCell ref="([A-Z]+)(\d+):([A-Z]+)(\d+)"', sheet_xml):
        ranges.append(
            (
                column_index(match.group(1)),
                int(match.group(2)),
                column_index(match.group(3)),
                int(match.group(4)),
            )
        )
    return ranges


def check_targets(path: Path, targets: list[str]) -> None:
    """書き込み先がすべて書けるセル（シートがあり、結合セルなら左上、計算式でない）か。"""
    with zipfile.ZipFile(path) as archive:
        sheets = sheet_paths(archive)
        cache: dict[str, str] = {}
        for target in targets:
            sheet, ref = split_target(target)
            if sheet not in sheets:
                raise XlsxWriteError(f"sheet not found: {sheet}")
            xml = cache.setdefault(sheet, archive.read(sheets[sheet]).decode("utf-8"))
            _check_cell(xml, ref)


def _check_cell(sheet_xml: str, ref: str) -> None:
    letters, row = split_ref(ref)
    column = column_index(letters)
    for left, top, right, bottom in merged_ranges(sheet_xml):
        inside = left <= column <= right and top <= row <= bottom
        if inside and (column, row) != (left, top):
            raise XlsxWriteError(f"{ref} is inside a merged range but not its top-left cell")
    element = _find_cell(sheet_xml, ref)
    if element is not None and "<f" in element.group(0):
        raise XlsxWriteError(f"{ref} has a formula")


def _find_cell(sheet_xml: str, ref: str) -> re.Match[str] | None:
    return re.search(rf'<c r="{ref}"(?:\s[^>]*?)?(?:/>|>.*?</c>)', sheet_xml, re.S)


def _cell_xml(ref: str, value: object, style: str | None) -> str:
    style_attr = f' s="{style}"' if style is not None else ""
    if isinstance(value, bool):
        raise XlsxWriteError(f"{ref}: booleans are not supported; format them as text")
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"{style_attr}><v>{value}</v></c>'
    text = escape(str(value))
    preserve = ' xml:space="preserve"' if text != text.strip() or "\n" in text else ""
    return f'<c r="{ref}"{style_attr} t="inlineStr"><is><t{preserve}>{text}</t></is></c>'


def _set_cell(sheet_xml: str, ref: str, value: object) -> str:
    _check_cell(sheet_xml, ref)
    existing = _find_cell(sheet_xml, ref)
    if existing is not None:
        style = re.search(r'\ss="(\d+)"', existing.group(0).split(">", 1)[0])
        new = _cell_xml(ref, value, style.group(1) if style else None)
        return sheet_xml[: existing.start()] + new + sheet_xml[existing.end() :]
    letters, row_number = split_ref(ref)
    new = _cell_xml(ref, value, None)
    row = re.search(rf'<row r="{row_number}"(?:\s[^>]*?)?(?:/>|>.*?</row>)', sheet_xml, re.S)
    if row is None:
        return _insert_row(sheet_xml, row_number, new)
    row_xml = row.group(0)
    if row_xml.endswith("/>"):
        replaced = row_xml[:-2] + ">" + new + "</row>"
    else:
        column = column_index(letters)
        position = row_xml.rindex("</row>")
        for cell in re.finditer(r'<c r="([A-Z]+)\d+"', row_xml):
            if column_index(cell.group(1)) > column:
                position = cell.start()
                break
        replaced = row_xml[:position] + new + row_xml[position:]
    return sheet_xml[: row.start()] + replaced + sheet_xml[row.end() :]


def _insert_row(sheet_xml: str, row_number: int, cell: str) -> str:
    new_row = f'<row r="{row_number}">{cell}</row>'
    if "<sheetData/>" in sheet_xml:
        return sheet_xml.replace("<sheetData/>", f"<sheetData>{new_row}</sheetData>", 1)
    for row in re.finditer(r'<row r="(\d+)"', sheet_xml):
        if int(row.group(1)) > row_number:
            return sheet_xml[: row.start()] + new_row + sheet_xml[row.start() :]
    end = sheet_xml.index("</sheetData>")
    return sheet_xml[:end] + new_row + sheet_xml[end:]


def write_cells(source: Path, destination: Path, values: Mapping[str, object]) -> None:
    """source のブックを destination に写し、values（「シート!A1」→ 値）のセルだけを書き換える。"""
    by_sheet: dict[str, dict[str, object]] = {}
    for target, value in values.items():
        sheet, ref = split_target(target)
        by_sheet.setdefault(sheet, {})[ref] = value
    with zipfile.ZipFile(source) as archive:
        paths = sheet_paths(archive)
        missing = [sheet for sheet in by_sheet if sheet not in paths]
        if missing:
            raise XlsxWriteError(f"sheet not found: {', '.join(missing)}")
        edited: dict[str, bytes] = {}
        for sheet, cells in by_sheet.items():
            xml = archive.read(paths[sheet]).decode("utf-8")
            for ref, value in cells.items():
                xml = _set_cell(xml, ref, value)
            edited[paths[sheet]] = xml.encode("utf-8")
        with zipfile.ZipFile(destination, "w") as output:
            for item in archive.infolist():
                data = edited.get(item.filename, archive.read(item.filename))
                output.writestr(item, data)
