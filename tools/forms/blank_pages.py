"""公式の様式を空のまま PDF にし、頁数・埋め込みの字体・代わりの字体を出す（開発用。字体の調べ）。

手元（web のコンテナ）とクラウド（RUNNER=host）で同じ形の結果を出し、見比べるために使う
（docs/cloud-session.md の 6）。変換は生成と同じ services.form_convert.convert（.docx と .xlsx
だけ。.doc・.xlsm の original は対象外）。様式のファイルは変えない（一時の写しを変換する）。

使い方（backend/ で。make blank-pages でも同じ。FORMS="1-23 1-5" で様式を絞る、MD=1 で Markdown）:
    docker compose exec web python ../tools/forms/blank_pages.py [--markdown] [様式ID ...]
    tools/cloud/host_run.sh python ../tools/forms/blank_pages.py [--markdown] [様式ID ...]

出力（タブ区切り。--markdown なら表）:
- 環境: LibreOffice の版、字体の規則（backend/config/fonts/99-form-fonts.conf）の SHA-256
- 頁数と字体: 様式・版・ファイル・頁数・PDF に埋め込まれた字体
- 代わりの字体: 文書が求める字体（元の字体名）と、PDF で使われた字体が違う所。字ごとに、
  文書の書式（直接の指定・スタイル・既定・テーマ）から求める字体を決め、PDF の同じ字の字体と比べる。
  理由は「規則が無い」（元の字体が無く、置き換えの規則も無い）、「置き換え先に字が無い」（規則の
  置き換え先や元の字体に、その字が無い）、「文書の文字に無い」（番号・記号など、本文の文字に無い字）。
  字の当てはめは近似（同じ字が複数の字体で出るときは、PDF と同じになる指定を優先する）。
  元の字体名に［テーマ］が付くものは、テーマの指定（minorEastAsia など）を Word の解き方で
  字体名にしたもの（LibreOffice は別の字体に解くことがある）。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
FORMS = REPO / "templates" / "forms"
RULES = BACKEND / "config" / "fonts" / "99-form-fonts.conf"
SOURCE_STEMS = ("original", "template", "converted", "extracted")

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
X = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
SCRIPTS = {"ja": "Jpan", "zh": "Hans", "ko": "Hang"}
# テーマの指定から Word の解き方で求めた字体名の印（LibreOffice は別に解くことがある）
THEMED = "［テーマ］"


def key(name: str) -> str:
    """字体名を比べる形にする（全角を半角に、大文字を小文字に、空白・-・_ を除く）。"""
    return re.sub(r"[\s\-_]", "", unicodedata.normalize("NFKC", name).lower())


def embedded_family(base_font: str) -> str:
    """PDF の BaseFont（/ABCDEF+NotoSansCJKjp-Regular）から字体の名前（NotoSansCJKjp）。"""
    name = re.sub(r"^[A-Z]{6}\+", "", base_font.lstrip("/"))
    return name.split("-")[0] if "-" in name else name


def is_east_asian(char: str) -> bool:
    code = ord(char)
    return (
        0x2E80 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0xFE30 <= code <= 0xFE4F
        or 0xFF00 <= code <= 0xFFEF
        or 0x20000 <= code <= 0x3FFFF
    )


# --- 字体の置き換えの規則と、入っている字体 ---------------------------------------------------


def read_rules(path: Path = RULES) -> dict[str, str]:
    """99-form-fonts.conf の alias（元の字体名 → 置き換え先）。鍵は key() の形。"""
    rules: dict[str, str] = {}
    for alias in ET.parse(path).getroot().iter("alias"):
        family = alias.findtext("family")
        prefer = alias.findtext("prefer/family")
        if family and prefer:
            rules[key(family)] = prefer
    return rules


def installed_families() -> set[str]:
    if shutil.which("fc-list") is None:
        return set()
    result = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True, check=True)
    return {key(name) for line in result.stdout.splitlines() for name in line.split(",") if name}


@dataclass
class Fonts:
    rules: dict[str, str]
    installed: set[str]

    def target(self, requested: str) -> tuple[str | None, str]:
        """求める字体の置き換え先と、その種類（規則・そのまま・規則なし）。

        Word の字体名は別名を ; で並べることがある（ＭＳ 明朝;MS Mincho）。どれかに当たればよい。
        """
        names = [name for name in requested.replace(THEMED, "").split(";") if name]
        for name in names:
            if key(name) in self.rules:
                return self.rules[key(name)], "規則"
        for name in names:
            if key(name) in self.installed:
                return name, "そのまま"
        return None, "規則なし"


# --- 文書の字ごとの、求める字体 ---------------------------------------------------------------

CharFonts = dict[str, Counter[str]]


@dataclass
class Theme:
    latin: dict[str, str] = field(default_factory=dict)  # minor・major → 字体名
    east_asian: dict[str, str] = field(default_factory=dict)

    @classmethod
    def read(cls, xml: bytes | None, east_asian_lang: str) -> Theme:
        theme = cls()
        if xml is None:
            return theme
        script = SCRIPTS.get(east_asian_lang.split("-")[0], "Jpan")
        root = ET.fromstring(xml)
        for kind, tag in (("major", "majorFont"), ("minor", "minorFont")):
            font = root.find(f".//{A}{tag}")
            if font is None:
                continue
            latin = font.find(f"{A}latin")
            theme.latin[kind] = latin.get("typeface", "") if latin is not None else ""
            east_asian = font.find(f"{A}ea")
            typeface = east_asian.get("typeface", "") if east_asian is not None else ""
            if not typeface:
                for item in font.findall(f"{A}font"):
                    if item.get("script") == script:
                        typeface = item.get("typeface", "")
            theme.east_asian[kind] = typeface
        return theme

    def resolve(self, value: str) -> str:
        """テーマの指定（minorEastAsia・+mn-ea など）を字体名に。"""
        kind = "major" if value.startswith(("major", "+mj")) else "minor"
        east_asian = value.endswith(("EastAsia", "-ea"))
        return (self.east_asian if east_asian else self.latin).get(kind, "")


SLOTS = ("ascii", "hAnsi", "eastAsia")


def _rfonts(rpr: ET.Element | None, theme: Theme) -> dict[str, str]:
    """rPr の rFonts を、枠（ascii・hAnsi・eastAsia）ごとの字体名に（テーマの指定が優先）。"""
    fonts: dict[str, str] = {}
    element = rpr.find(f"{W}rFonts") if rpr is not None else None
    if element is None:
        return fonts
    for slot in SLOTS:
        themed = element.get(f"{W}{slot}Theme")
        if themed:
            fonts[slot] = (theme.resolve(themed) or f"（{themed} が空）") + THEMED
        elif element.get(f"{W}{slot}"):
            fonts[slot] = element.get(f"{W}{slot}", "")
    return fonts


class Docx:
    def __init__(self, book: zipfile.ZipFile) -> None:
        names = set(book.namelist())
        settings = (
            ET.fromstring(book.read("word/settings.xml")) if "word/settings.xml" in names else None
        )
        lang = settings.find(f"{W}themeFontLang") if settings is not None else None
        east_asian_lang = lang.get(f"{W}eastAsia", "ja-JP") if lang is not None else "ja-JP"
        theme_xml = book.read("word/theme/theme1.xml") if "word/theme/theme1.xml" in names else None
        self.theme = Theme.read(theme_xml, east_asian_lang)
        self.defaults: dict[str, str] = {}
        self.styles: dict[str, tuple[str | None, dict[str, str]]] = {}
        self.default_paragraph: str | None = None
        if "word/styles.xml" in names:
            styles = ET.fromstring(book.read("word/styles.xml"))
            self.defaults = _rfonts(styles.find(f"{W}docDefaults/{W}rPrDefault/{W}rPr"), self.theme)
            for style in styles.findall(f"{W}style"):
                style_id = style.get(f"{W}styleId", "")
                based = style.find(f"{W}basedOn")
                parent = based.get(f"{W}val") if based is not None else None
                self.styles[style_id] = (parent, _rfonts(style.find(f"{W}rPr"), self.theme))
                if style.get(f"{W}type") == "paragraph" and style.get(f"{W}default") == "1":
                    self.default_paragraph = style_id
        self.parts = [
            ET.fromstring(book.read(name))
            for name in sorted(names)
            if re.fullmatch(r"word/(document|header\d*|footer\d*)\.xml", name)
        ]

    def _style_chain(self, style_id: str | None) -> list[dict[str, str]]:
        chain: list[dict[str, str]] = []
        seen: set[str] = set()
        while style_id and style_id in self.styles and style_id not in seen:
            seen.add(style_id)
            parent, fonts = self.styles[style_id]
            chain.insert(0, fonts)
            style_id = parent
        return chain

    def char_fonts(self) -> CharFonts:
        result: CharFonts = defaultdict(Counter)
        for part in self.parts:
            self._walk(part, self.default_paragraph, result)
        return result

    def _walk(self, element: ET.Element, paragraph_style: str | None, result: CharFonts) -> None:
        if element.tag == f"{W}p":
            style = element.find(f"{W}pPr/{W}pStyle")
            paragraph_style = style.get(f"{W}val") if style is not None else self.default_paragraph
        if element.tag == f"{W}r":
            self._run(element, paragraph_style, result)
        for child in element:
            self._walk(child, paragraph_style, result)

    def _run(self, run: ET.Element, paragraph_style: str | None, result: CharFonts) -> None:
        rpr = run.find(f"{W}rPr")
        run_style = rpr.find(f"{W}rStyle") if rpr is not None else None
        fonts = dict(self.defaults)
        layers = [
            *self._style_chain(paragraph_style),
            *self._style_chain(run_style.get(f"{W}val") if run_style is not None else None),
            _rfonts(rpr, self.theme),
        ]
        for layer in layers:
            fonts.update(layer)
        element = rpr.find(f"{W}rFonts") if rpr is not None else None
        hint = element.get(f"{W}hint") if element is not None else None
        text = "".join(t.text or "" for t in run.findall(f"{W}t"))
        for char in text:
            if char.isspace():
                continue
            if is_east_asian(char) or (hint == "eastAsia" and ord(char) >= 0x80):
                slot = "eastAsia"
            else:
                slot = "ascii" if ord(char) < 0x80 else "hAnsi"
            result[char][fonts.get(slot) or "（指定なし）"] += 1


class Xlsx:
    def __init__(self, book: zipfile.ZipFile) -> None:
        self.book = book
        names = set(book.namelist())
        theme_xml = book.read("xl/theme/theme1.xml") if "xl/theme/theme1.xml" in names else None
        self.theme = Theme.read(theme_xml, "ja-JP")
        styles = ET.fromstring(book.read("xl/styles.xml"))
        self.fonts = [
            (font.find(f"{X}name").get("val", "") if font.find(f"{X}name") is not None else "")  # type: ignore[union-attr]
            for font in styles.findall(f"{X}fonts/{X}font")
        ]
        self.cell_fonts = [int(xf.get("fontId", "0")) for xf in styles.findall(f"{X}cellXfs/{X}xf")]
        self.shared: list[list[tuple[str | None, str]]] = []
        if "xl/sharedStrings.xml" in names:
            for item in ET.fromstring(book.read("xl/sharedStrings.xml")).findall(f"{X}si"):
                runs = item.findall(f"{X}r")
                if runs:
                    self.shared.append(
                        [
                            (
                                run.find(f"{X}rPr/{X}rFont").get("val")  # type: ignore[union-attr]
                                if run.find(f"{X}rPr/{X}rFont") is not None
                                else None,
                                "".join(t.text or "" for t in run.iter(f"{X}t")),
                            )
                            for run in runs
                        ]
                    )
                else:
                    self.shared.append([(None, "".join(t.text or "" for t in item.iter(f"{X}t")))])
        self.names = sorted(names)

    def char_fonts(self) -> CharFonts:
        result: CharFonts = defaultdict(Counter)
        for name in self.names:
            if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name):
                self._sheet(ET.fromstring(self.book.read(name)), result)
            elif re.fullmatch(r"xl/drawings/drawing\d+\.xml", name):
                self._drawing(ET.fromstring(self.book.read(name)), result)
        return result

    def _sheet(self, sheet: ET.Element, result: CharFonts) -> None:
        for cell in sheet.iter(f"{X}c"):
            style = int(cell.get("s", "0"))
            font_id = self.cell_fonts[style] if style < len(self.cell_fonts) else 0
            cell_font = self.fonts[font_id] if font_id < len(self.fonts) else ""
            kind = cell.get("t")
            value = cell.findtext(f"{X}v") or ""
            if kind == "s" and value.isdigit() and int(value) < len(self.shared):
                runs = self.shared[int(value)]
            elif kind == "inlineStr":
                runs = [(None, "".join(t.text or "" for t in cell.iter(f"{X}t")))]
            else:
                runs = [(None, value)]
            for font, text in runs:
                for char in text:
                    if not char.isspace():
                        result[char][font or cell_font or "（指定なし）"] += 1

    def _drawing(self, drawing: ET.Element, result: CharFonts) -> None:
        for run in drawing.iter(f"{A}r"):
            rpr = run.find(f"{A}rPr")
            text = run.findtext(f"{A}t") or ""
            for char in text:
                if char.isspace():
                    continue
                tag = "ea" if is_east_asian(char) else "latin"
                element = rpr.find(f"{A}{tag}") if rpr is not None else None
                typeface = element.get("typeface", "") if element is not None else ""
                if not typeface or typeface.startswith("+"):
                    typeface = self.theme.resolve(
                        typeface or ("+mn-ea" if tag == "ea" else "+mn-lt")
                    )
                result[char][typeface or "（指定なし）"] += 1


def char_fonts(path: Path) -> CharFonts:
    with zipfile.ZipFile(path) as book:
        return (Docx(book) if path.suffix == ".docx" else Xlsx(book)).char_fonts()


# --- PDF の字ごとの、埋め込みの字体 -----------------------------------------------------------


def pdf_chars(pdf: Path) -> tuple[int, Counter[tuple[str, str]]]:
    """頁数と、(埋め込みの字体, 字) ごとの数。"""
    from pypdf import PdfReader

    reader = PdfReader(str(pdf))
    found: Counter[tuple[str, str]] = Counter()
    for page in reader.pages:

        def visit(text: str, cm: object, tm: object, font: object, size: object) -> None:
            if not isinstance(font, dict) or "/BaseFont" not in font:
                return
            family = embedded_family(str(font["/BaseFont"]))
            for char in text:
                if not char.isspace():
                    found[(family, char)] += 1

        page.extract_text(visitor_text=visit)
    return len(reader.pages), found


# --- 突き合わせ --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Fallback:
    requested: str
    target: str
    chosen: str
    reason: str


def fallbacks(
    requested_by_char: CharFonts, embedded: Counter[tuple[str, str]], fonts: Fonts
) -> dict[Fallback, Counter[str]]:
    result: dict[Fallback, Counter[str]] = defaultdict(Counter)
    for (family, char), count in embedded.items():
        candidates = requested_by_char.get(char)
        if not candidates:
            result[Fallback("（文書の文字に無い）", "", family, "文書の文字に無い")][char] += count
            continue
        targets = {name: fonts.target(name) for name in candidates}
        if any(target and key(target) == key(family) for target, _ in targets.values()):
            continue
        requested = candidates.most_common(1)[0][0]
        target, kind = targets[requested]
        reason = "規則が無い" if target is None else "置き換え先に字が無い"
        label = f"{target}（{kind}）" if target else "—"
        result[Fallback(requested, label, family, reason)][char] += count
    return result


# --- 全体 --------------------------------------------------------------------------------------


@dataclass
class Row:
    form: str
    version: str
    source: str
    pages: int | str
    families: list[str]
    fallbacks: dict[Fallback, Counter[str]]


def sources(selected: Iterable[str]) -> Iterator[tuple[str, str, Path]]:
    wanted = set(selected)
    for folder in sorted(path for path in FORMS.glob("*/*") if path.is_dir()):
        form, version = folder.parent.name, folder.name
        if wanted and form not in wanted:
            continue
        for path in sorted(folder.iterdir()):
            if path.stem in SOURCE_STEMS and path.suffix in (".docx", ".xlsx"):
                yield form, version, path


def convert_blank(path: Path) -> Path:
    from services import form_convert

    workdir = Path(tempfile.mkdtemp(prefix="blank-pages-"))
    copy = workdir / f"blank{path.suffix}"
    shutil.copyfile(path, copy)
    return form_convert.convert(copy)


def collect(selected: Iterable[str], fonts: Fonts) -> Iterator[Row]:
    for form, version, path in sources(selected):
        try:
            pdf = convert_blank(path)
        except Exception as error:  # noqa: BLE001  変換できない版も一覧に残す
            yield Row(form, version, path.name, f"変換できない（{type(error).__name__}）", [], {})
            continue
        pages, embedded = pdf_chars(pdf)
        shutil.rmtree(pdf.parent, ignore_errors=True)
        families = sorted({family for family, _ in embedded})
        yield Row(
            form, version, path.name, pages, families, fallbacks(char_fonts(path), embedded, fonts)
        )


def environment() -> list[tuple[str, str]]:
    from services import form_convert

    return [
        ("LibreOffice", form_convert.converter_version() or "（無い）"),
        ("字体の規則 SHA-256", hashlib.sha256(RULES.read_bytes()).hexdigest()),
        ("RUNNER", os.environ.get("RUNNER", "docker")),
    ]


def table(title: str, header: list[str], rows: list[list[str]], markdown: bool) -> str:
    if markdown:
        lines = [f"### {title}", "", "| " + " | ".join(header) + " |"]
        lines.append("|" + "---|" * len(header))
        lines += [
            "| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |" for row in rows
        ]
    else:
        lines = [f"# {title}", "\t".join(header), *("\t".join(row) for row in rows)]
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--markdown", action="store_true")
    parser.add_argument("forms", nargs="*", help="様式 ID（省けば全部）")
    options = parser.parse_args(argv)

    sys.path.insert(0, str(BACKEND))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()
    fonts = Fonts(read_rules(), installed_families())
    rows = list(collect(options.forms, fonts))
    print(table("環境", ["項目", "値"], [list(item) for item in environment()], options.markdown))
    print(
        table(
            "頁数と字体",
            ["様式", "版", "ファイル", "頁数", "埋め込みの字体"],
            [[r.form, r.version, r.source, str(r.pages), ", ".join(r.families)] for r in rows],
            options.markdown,
        )
    )
    fallback_rows = [
        [
            r.form,
            r.version,
            r.source,
            fb.requested,
            fb.target,
            fb.chosen,
            str(sum(chars.values())),
            "".join(char for char, _ in chars.most_common(12)),
            fb.reason,
        ]
        for r in rows
        for fb, chars in sorted(r.fallbacks.items(), key=lambda item: -sum(item[1].values()))
    ]
    header = [
        "様式",
        "版",
        "ファイル",
        "元の字体名",
        "置き換え先",
        "選ばれた字体",
        "字の数",
        "字の例",
        "理由",
    ]
    print(table("代わりの字体", header, fallback_rows, options.markdown))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
