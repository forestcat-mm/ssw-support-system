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

import math
import posixpath
import re
import unicodedata
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

CELL_REF = re.compile(r"^([A-Z]{1,3})(\d+)$")
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


class XlsxWriteError(Exception):
    """書き込めないセル（計算式・結合セルの左上以外・存在しないシート）。"""


@dataclass(frozen=True)
class SizedText:
    """字の大きさを指定して書く文字（インライン文字列のリッチテキスト。セルの書式は変えない）。"""

    text: str
    size: float
    font: str
    charset: str | None = None


@dataclass(frozen=True)
class CellFont:
    """セルの書式のフォントと、書き込み先（結合範囲）の幅。"""

    name: str
    size: float
    charset: str | None
    width_pt: float
    height_pt: float = 0.0  # 書き込み先（結合範囲）の高さ（行の高さの合計。fit と wrap で使う）


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


# 計算式の中のセル・範囲の参照（シート名つき・列全体・行全体を含む）
_REF = re.compile(
    r"(?:(?P<sheet>'(?:[^']|'')+'|[^\s!'\"(),:;+\-*/&=<>^]+)!)?"
    r"(?:\$?(?P<c1>[A-Z]{1,3})\$?(?P<r1>\d+)(?::\$?(?P<c2>[A-Z]{1,3})\$?(?P<r2>\d+))?"
    r"|\$?(?P<cc1>[A-Z]{1,3}):\$?(?P<cc2>[A-Z]{1,3})"
    r"|\$?(?P<rr1>\d+):\$?(?P<rr2>\d+))"
)
_MAX_ROW = 1_048_576
ANY_SHEET = "*"
_MAX_COLUMN = 16_384


def _areas(formula: str, own_sheet: str) -> list[tuple[str, int, int, int, int]]:
    """計算式が参照する範囲（シート名・列の始め・行の始め・列の終わり・行の終わり）。"""
    text = re.sub(r'"(?:[^"]|"")*"', "", formula)  # 文字列の定数は除く
    areas = []
    for match in _REF.finditer(text):
        sheet = match.group("sheet")
        sheet_name = sheet.strip("'").replace("''", "'") if sheet else own_sheet
        if match.group("c1"):
            c1, r1 = column_index(match.group("c1")), int(match.group("r1"))
            c2 = column_index(match.group("c2")) if match.group("c2") else c1
            r2 = int(match.group("r2")) if match.group("r2") else r1
        elif match.group("cc1"):
            c1, c2 = column_index(match.group("cc1")), column_index(match.group("cc2"))
            r1, r2 = 1, _MAX_ROW
        else:
            r1, r2 = int(match.group("rr1")), int(match.group("rr2"))
            c1, c2 = 1, _MAX_COLUMN
        areas.append((sheet_name, min(c1, c2), min(r1, r2), max(c1, c2), max(r1, r2)))
    return areas


def print_areas(archive: zipfile.ZipFile) -> dict[str, tuple[int, int, int, int]]:
    """シート名 → 印刷範囲（列の始め・行の始め・列の終わり・行の終わり）。"""
    workbook = archive.read("xl/workbook.xml").decode("utf-8")
    names = [
        _unescape(re.search(r'name="([^"]+)"', tag).group(1))  # type: ignore[union-attr]
        for tag in re.findall(r"<sheet [^>]*/>", workbook)
    ]
    result = {}
    for match in re.finditer(r"<definedName\b([^>]*)>([^<]*)</definedName>", workbook):
        attrs = match.group(1)
        if 'name="_xlnm.Print_Area"' not in attrs:
            continue
        local = re.search(r'localSheetId="(\d+)"', attrs)
        if local is None or int(local.group(1)) >= len(names):
            continue
        areas = _areas(_unescape(match.group(2)), names[int(local.group(1))])
        if areas:
            _sheet, c1, r1, c2, r2 = areas[0]
            result[names[int(local.group(1))]] = (c1, r1, c2, r2)
    return result


def ignorable_formula_cells(archive: zipfile.ZipFile) -> set[str]:
    """参照の検査から外す計算式のセル（ADR-0018）。

    キャッシュの値がエラー（t="e"）で、印刷範囲の外にある計算式。結果が PDF に出ず、書いた値で
    変わることもない（第1-17号の DA670 の壊れた式 `+#REF!:…`）。
    """
    areas = print_areas(archive)
    result = set()
    for sheet, path in sheet_paths(archive).items():
        xml = archive.read(path).decode("utf-8")
        area = areas.get(sheet)
        for match in re.finditer(r'<c r="([A-Z]+)(\d+)"([^>]*?)(?:/>|>(.*?)</c>)', xml, re.S):
            if 't="e"' not in match.group(3) or "<f" not in (match.group(4) or ""):
                continue
            column, row = column_index(match.group(1)), int(match.group(2))
            inside = area is not None and (
                area[0] <= column <= area[2] and area[1] <= row <= area[3]
            )
            if not inside:
                result.add(f"{sheet}!{match.group(1)}{match.group(2)}")
    return result


def formula_areas(archive: zipfile.ZipFile) -> list[tuple[str, str, int, int, int, int]]:
    """ブックのすべての計算式が参照する範囲（計算式のあるセル・シート名・範囲）。

    共有の計算式（t="shared"）は、親の式の参照を共有の範囲の大きさだけ広げて安全側に見る。
    名前の定義（_xlnm. で始まる印刷範囲などを除く）も、参照される範囲として含める。
    """
    result = []
    for sheet, path in sheet_paths(archive).items():
        xml = archive.read(path).decode("utf-8")
        for match in re.finditer(r'<c r="([A-Z]+\d+)"[^>]*?(?:/>|>(.*?)</c>)', xml, re.S):
            formula = re.search(r"<f([^>]*)>([^<]*)</f>", match.group(2) or "")
            if formula is None:
                continue
            spread = re.search(r'ref="([A-Z]+)(\d+):([A-Z]+)(\d+)"', formula.group(1))
            width = height = 0
            if 't="shared"' in formula.group(1) and spread:
                width = column_index(spread.group(3)) - column_index(spread.group(1))
                height = int(spread.group(4)) - int(spread.group(2))
            for name, c1, r1, c2, r2 in _areas(_unescape(formula.group(2)), sheet):
                result.append((f"{sheet}!{match.group(1)}", name, c1, r1, c2 + width, r2 + height))
    workbook = archive.read("xl/workbook.xml").decode("utf-8")
    for defined in re.finditer(r"<definedName\b([^>]*)>([^<]*)</definedName>", workbook):
        label = re.search(r'\sname="([^"]*)"', defined.group(1))
        if label is None or label.group(1).startswith("_xlnm."):
            continue
        # シート名の無い参照は、どのシートのセルとも重なりうるものとして見る（安全側）
        for name, c1, r1, c2, r2 in _areas(_unescape(defined.group(2)), ANY_SHEET):
            result.append((f"name:{_unescape(label.group(1))}", name, c1, r1, c2, r2))
    return result


def targets_referenced_by_formulas(path: Path, targets: list[str]) -> list[str]:
    """書き込み先のうち、ブックの計算式（または名前の定義）が参照しているもの。

    キャッシュの値がエラーで印刷範囲の外にある計算式は見ない（ignorable_formula_cells）。
    """
    with zipfile.ZipFile(path) as archive:
        ignored = ignorable_formula_cells(archive)
        areas = [area for area in formula_areas(archive) if area[0] not in ignored]
    found = []
    for target in targets:
        sheet, ref = split_target(target)
        letters, row = split_ref(ref)
        column = column_index(letters)
        for source, name, c1, r1, c2, r2 in areas:
            hit = f"{target} <- {source}"
            inside = name in (sheet, ANY_SHEET) and c1 <= column <= c2 and r1 <= row <= r2
            if inside and hit not in found:
                found.append(hit)
    return found


def _find_cell(sheet_xml: str, ref: str) -> re.Match[str] | None:
    return re.search(rf'<c r="{ref}"(?:\s[^>]*?)?(?:/>|>.*?</c>)', sheet_xml, re.S)


def _cell_xml(ref: str, value: object, style: str | None) -> str:
    style_attr = f' s="{style}"' if style is not None else ""
    if isinstance(value, bool):
        raise XlsxWriteError(f"{ref}: booleans are not supported; format them as text")
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"{style_attr}><v>{value}</v></c>'
    if isinstance(value, SizedText):
        text = escape(value.text)
        charset = f'<charset val="{value.charset}"/>' if value.charset else ""
        run = (
            f'<r><rPr><sz val="{value.size:g}"/><rFont val="{escape(value.font)}"/>{charset}'
            f'</rPr><t xml:space="preserve">{text}</t></r>'
        )
        return f'<c r="{ref}"{style_attr} t="inlineStr"><is>{run}</is></c>'
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


# --- 字の大きさ（長い値を欄に収める。T31、ADR-0020）---------------------------------------

MAX_DIGIT_WIDTH_PX = 7  # 既定のフォント（ＭＳ Ｐゴシック 11pt）の数字の幅の近似
PADDING_PT = 3.0  # セルの左右の余白の近似


def _column_widths(sheet_xml: str) -> tuple[dict[int, float], float]:
    default = re.search(r'defaultColWidth="([\d.]+)"', sheet_xml)
    widths: dict[int, float] = {}
    for match in re.finditer(r"<col\b([^>]*)/>", sheet_xml):
        attrs = match.group(1)
        low = int(re.search(r'min="(\d+)"', attrs).group(1))  # type: ignore[union-attr]
        high = int(re.search(r'max="(\d+)"', attrs).group(1))  # type: ignore[union-attr]
        width = float(re.search(r'width="([\d.]+)"', attrs).group(1))  # type: ignore[union-attr]
        for column in range(low, min(high, 1000) + 1):
            widths[column] = width
    return widths, float(default.group(1)) if default else 8.43


def _row_heights(sheet_xml: str) -> tuple[dict[int, float], float]:
    """行の高さ（pt）。高さの指定の無い行は既定の高さ。"""
    default = re.search(r'<sheetFormatPr\b[^>]*defaultRowHeight="([\d.]+)"', sheet_xml)
    heights = {
        int(match.group(1)): float(match.group(2))
        for match in re.finditer(r'<row r="(\d+)"[^>]*?\sht="([\d.]+)"', sheet_xml)
    }
    return heights, float(default.group(1)) if default else 13.5


def _width_pt(width_chars: float) -> float:
    pixels = math.trunc(
        ((256 * width_chars + math.trunc(128 / MAX_DIGIT_WIDTH_PX)) / 256) * MAX_DIGIT_WIDTH_PX
    )
    return pixels * 0.75


def _fonts(styles_xml: str) -> tuple[list[tuple[str, float, str | None]], list[int]]:
    fonts_xml = re.search(r"<fonts[^>]*>(.*?)</fonts>", styles_xml, re.S)
    fonts = []
    for font in re.findall(
        r"<font\b[^>]*?(?:/>|>.*?</font>)", fonts_xml.group(1) if fonts_xml else "", re.S
    ):
        name = re.search(r'<name val="([^"]+)"', font)
        size = re.search(r'<sz val="([\d.]+)"', font)
        charset = re.search(r'<charset val="(\d+)"', font)
        fonts.append(
            (
                _unescape(name.group(1)) if name else "",
                float(size.group(1)) if size else 11.0,
                charset.group(1) if charset else None,
            )
        )
    xfs_xml = re.search(r"<cellXfs[^>]*>(.*?)</cellXfs>", styles_xml, re.S)
    xf_fonts = [
        int(match.group(1)) if match else 0
        for match in (
            re.search(r'fontId="(\d+)"', xf)
            for xf in re.findall(
                r"<xf\b[^>]*?(?:/>|>.*?</xf>)", xfs_xml.group(1) if xfs_xml else "", re.S
            )
        )
    ]
    return fonts, xf_fonts


def cell_fonts(
    path: Path, targets: list[str], areas: Mapping[str, str] | None = None
) -> dict[str, CellFont]:
    """書き込み先ごとのフォント（セルの書式）と幅（結合範囲の列の幅の合計。pt の近似）。

    areas は、結合されていないセルの字が右の空のセルに続けて描かれる範囲（例 "O44:BA44"。
    マッピングの fit_area。修正 1 回目 A6）。その範囲の幅と高さを使う。
    """
    areas = areas or {}
    result: dict[str, CellFont] = {}
    with zipfile.ZipFile(path) as archive:
        sheets = sheet_paths(archive)
        fonts, xf_fonts = _fonts(archive.read("xl/styles.xml").decode("utf-8"))
        cache: dict[str, str] = {}
        for target in targets:
            sheet, ref = split_target(target)
            xml = cache.setdefault(sheet, archive.read(sheets[sheet]).decode("utf-8"))
            letters, row = split_ref(ref)
            column = column_index(letters)
            right, bottom = column, row
            for left, top, end, last in merged_ranges(xml):
                if (left, top) == (column, row):
                    right, bottom = end, last
            if target in areas:
                end_letters, end_row = split_ref(areas[target].split(":")[1])
                right, bottom = column_index(end_letters), end_row
            widths, default = _column_widths(xml)
            width = sum(_width_pt(widths.get(c, default)) for c in range(column, right + 1))
            heights, default_height = _row_heights(xml)
            height = sum(heights.get(r, default_height) for r in range(row, bottom + 1))
            element = _find_cell(xml, ref)
            style = (
                re.search(r'\ss="(\d+)"', element.group(0).split(">", 1)[0]) if element else None
            )
            font_id = xf_fonts[int(style.group(1))] if style else 0
            name, size, charset = fonts[font_id] if font_id < len(fonts) else ("", 11.0, None)
            result[target] = CellFont(name, size, charset, width, height)
    return result


def text_em(text: str) -> float:
    """文字の幅（全角を 1、半角を 0.55 とした近似）。"""
    return sum(
        1.0 if unicodedata.east_asian_width(char) in ("F", "W", "A") else 0.55 for char in text
    )


def height_limit(font: CellFont) -> float:
    """書き込み先（結合範囲）の高さに 1 行が収まる字の大きさの上限（0.5pt 刻み。修正 1 回目 A6）。

    LibreOffice は行の高さからはみ出した字を切る（Excel は下の空の行に描く）。高さが分からない
    （0）ときは上限を設けない。
    """
    if font.height_pt <= 0:
        return font.size
    return math.floor(font.height_pt / LINE_SPACING * 2) / 2


def fitted_size(text: str, font: CellFont, min_size: float) -> tuple[float, bool]:
    """1 行で欄に収まる字の大きさ（0.5pt 刻み。元より大きくしない）と、下限でも収まらないか。

    幅と、書き込み先の高さ（1 行の高さ = 字の大きさ × LINE_SPACING）の両方を満たす大きさにする。
    """
    em = text_em(text)
    room = max(font.width_pt - PADDING_PT, 1.0)
    size = min(font.size, height_limit(font))
    if em > 0 and em * size > room:
        size = math.floor(room / em * 2) / 2
    if size < min_size:
        return min_size, True
    return size, False


# --- 複数行（長い自由記述。T35-D11、ADR-0024）---------------------------------------------

LINE_SPACING = 1.25  # 行の高さ ÷ 字の大きさの近似（置き換え先の明朝・ゴシック）


def wrap_lines(text: str, font: CellFont, size: float) -> list[str]:
    """欄の幅で改行した行（もとの改行は残す。字の大きさ size のとき）。"""
    room = max(font.width_pt - PADDING_PT, 1.0) / size  # 1 行に入る幅（em）
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        line, used = "", 0.0
        for char in paragraph:
            em = text_em(char)
            if line and used + em > room:
                lines.append(line)
                line, used = "", 0.0
            line += char
            used += em
        lines.append(line)
    return lines


def wrapped_size(text: str, font: CellFont, min_size: float) -> tuple[float, bool]:
    """欄の幅で改行して、結合範囲の高さに収まる字の大きさ（0.5pt 刻み）と、下限でも収まらないか。"""
    size = font.size
    while True:
        lines = wrap_lines(text, font, size)
        if len(lines) * size * LINE_SPACING <= font.height_pt:
            return size, False
        if size - 0.5 < min_size:
            return size, True
        size -= 0.5
