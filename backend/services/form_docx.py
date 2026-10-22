"""Word の様式に差し込む（docxtpl。T29、ADR-0018）。

template.docx は、入管の original の写しに差込タグ（{{ 名前 }}）を置くことだけをしたもの。
タグを外した本文が original の本文と同じことをテストで確かめる（tests/forms_engine）。
"""

from __future__ import annotations

import functools
import re
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docxtpl import DocxTemplate  # type: ignore[import-untyped]
from lxml import etree  # type: ignore[import-untyped]  # docxtpl（python-docx）の依存

TAG = re.compile(r"\{\{.*?\}\}|\{%.*?%\}")


def render(template: Path, destination: Path, context: Mapping[str, object]) -> None:
    document = DocxTemplate(str(template))
    document.render(dict(context), autoescape=True)
    document.save(str(destination))


def strip_tags(text: str) -> str:
    """差込タグを外した文字列（タグ以外を変えていないかの確かめに使う）。"""
    return TAG.sub("", text)


BODY_SECTION = re.compile(r"<w:sectPr\b(?:(?!<w:sectPr\b).)*?</w:sectPr>\s*</w:body>", re.S)
PAGE_BREAK = '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'


def append_body(document: Path, extra: Path) -> None:
    """document の本文の末尾に、改ページを挟んで extra の本文を綴じる（補足の頁。ADR-0027 の追記）。

    extra は同じ部品（スタイル・用紙）から作った文書で、画像などの関係（rels）を持たないこと。
    extra の最後の節の設定は使わず、document の節の設定のままにする。
    """
    with zipfile.ZipFile(extra) as archive:
        extra_xml = archive.read("word/document.xml").decode("utf-8")
        if any(name.startswith("word/media/") for name in archive.namelist()):
            raise ValueError("the supplement must not have media")
    start = extra_xml.index("<w:body>") + len("<w:body>")
    section = BODY_SECTION.search(extra_xml)
    if section is None:
        raise ValueError("the supplement has no section")
    body = extra_xml[start : section.start()]
    with zipfile.ZipFile(document) as archive:
        items = [(item, archive.read(item.filename)) for item in archive.infolist()]
    with zipfile.ZipFile(document, "w", zipfile.ZIP_DEFLATED) as output:
        for item, data in items:
            if item.filename == "word/document.xml":
                xml = data.decode("utf-8")
                match = BODY_SECTION.search(xml)
                if match is None:
                    raise ValueError("the document has no section")
                xml = xml[: match.start()] + PAGE_BREAK + body + xml[match.start() :]
                data = xml.encode("utf-8")
            output.writestr(item, data)


# PDF にする一時の写しでだけ変える頁の組み方（manifest の pdf_layout。ADR-0032 の例外の一覧）
PDF_LAYOUT_KEYS = {"line_pitch", "margin_top", "margin_bottom", "font_sizes", "note"}
MIN_BODY_HALF_POINTS = 18  # 本文の字は 9pt を下回らない


def pdf_layout_errors(layout: Mapping[str, object]) -> list[str]:
    errors = [f"unknown key: {key}" for key in layout if key not in PDF_LAYOUT_KEYS]
    for key in ("line_pitch", "margin_top", "margin_bottom"):
        value = layout.get(key)
        if value is not None and (not isinstance(value, int) or value <= 0):
            errors.append(f"{key} must be a positive integer (twips)")
    sizes = layout.get("font_sizes", {})
    if not isinstance(sizes, dict):
        return [*errors, "font_sizes must be an object"]
    for old, new in sizes.items():
        if not str(old).isdigit() or not isinstance(new, int) or new < MIN_BODY_HALF_POINTS:
            errors.append(f"font_sizes: {old} -> {new} (half points, at least 18 = 9pt)")
    return errors


def with_pdf_layout(source: Path, destination: Path, layout: Mapping[str, object]) -> None:
    """source を destination に写し、本文の行のグリッドの間隔・上下の余白・字の大きさを変える。

    差し込んだ文書（保存するもの）は変えない。PDF に変換する直前の一時の写しでだけ使う。
    font_sizes は半ポイントの大きさの置き換え（例: {"22": 20} は 11pt → 10pt）。
    """
    pitch = layout.get("line_pitch")
    top = layout.get("margin_top")
    bottom = layout.get("margin_bottom")
    sizes = {str(old): str(new) for old, new in dict(layout.get("font_sizes") or {}).items()}  # type: ignore[call-overload]
    with zipfile.ZipFile(source) as archive:
        items = [(item, archive.read(item.filename)) for item in archive.infolist()]
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as output:
        for item, data in items:
            if item.filename == "word/document.xml":
                xml = data.decode("utf-8")
                if pitch:
                    xml = re.sub(r'(<w:docGrid\b[^>]*?w:linePitch=")\d+"', rf'\g<1>{pitch}"', xml)
                for name, value in (("top", top), ("bottom", bottom)):
                    if value:
                        xml = re.sub(rf'(<w:pgMar\b[^>]*?w:{name}=")\d+"', rf'\g<1>{value}"', xml)
                if sizes:
                    xml = re.sub(
                        r'(<w:sz(?:Cs)? w:val=")(\d+)"',
                        lambda match: (
                            f'{match.group(1)}{sizes.get(match.group(2), match.group(2))}"'
                        ),
                        xml,
                    )
                data = xml.encode("utf-8")
            output.writestr(item, data)


# PDF にする一時の写しでだけ、中国語の字体の run の東アジアの言語を zh-CN にする（ADR-0048）。
# LibreOffice は字形を run の言語で選ぶ（言語が ja なら、SimSun でも日本の字形）。Word は字体の
# 字形で描くので、写しで言語をそろえて Word の見た目に近づける。保存する文書は変えない。
CHINESE_FONTS = frozenset(
    {
        "SimSun",
        "宋体",
        "NSimSun",
        "新宋体",
        "SimHei",
        "黑体",
        "DengXian",
        "等线",
        "Microsoft YaHei",
        "微软雅黑",
        "KaiTi",
        "楷体",
        "FangSong",
        "仿宋",
        "Noto Serif CJK SC",
        "Noto Sans CJK SC",
    }
)
CHINESE = "zh-CN"
WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
# ひらがな・カタカナ（中黒「・」U+30FB は中国語の文でも区切りに使うので、かなとして数えない）
KANA = re.compile(r"[\u3040-\u309f\u30a0-\u30fa\u30fc-\u30ff\u31f0-\u31ff\uff66-\uff9f]")
# w:rPr の中で w:lang より後ろに置く要素（スキーマの順）
AFTER_LANG = ("eastAsianLayout", "specVanish", "oMath", "rPrChange")
TEXT_PARTS = re.compile(r"word/(document|header\d*|footer\d*)\.xml")


@dataclass(frozen=True)
class LanguageFix:
    """言語を書き換えた run の数と、かなを含むので書き換えなかった run の数。"""

    changed: int = 0
    skipped_kana: int = 0


def _w(tag: str) -> str:
    return f"{{{WORD_NS}}}{tag}"


class _Styles:
    """styles.xml の東アジアの字体と言語（basedOn をたどる）。"""

    def __init__(self, xml: bytes | None) -> None:
        self.styles: dict[str, Any] = {}
        self.default: dict[str, str] = {}
        self.default_paragraph: str | None = None
        if xml is None:
            return
        root = etree.fromstring(xml)
        for style in root.iter(_w("style")):
            style_id = style.get(_w("styleId"))
            if style_id:
                self.styles[style_id] = style
                if style.get(_w("type")) == "paragraph" and style.get(_w("default")) == "1":
                    self.default_paragraph = style_id
        defaults = root.find(f"{_w('docDefaults')}/{_w('rPrDefault')}/{_w('rPr')}")
        self.default = _font_and_lang(defaults)

    @functools.cache  # noqa: B019  1 つの文書の間だけ使う
    def chain(self, style_id: str | None) -> tuple[tuple[str, str], ...]:
        seen: list[str] = []
        props: dict[str, str] = {}
        while style_id and style_id in self.styles and style_id not in seen:
            seen.append(style_id)
            style = self.styles[style_id]
            for name, value in _font_and_lang(style.find(_w("rPr"))).items():
                props.setdefault(name, value)
            based = style.find(_w("basedOn"))
            style_id = based.get(_w("val")) if based is not None else None
        return tuple(props.items())


def _font_and_lang(rpr: Any) -> dict[str, str]:
    props: dict[str, str] = {}
    if rpr is None:
        return props
    fonts = rpr.find(_w("rFonts"))
    if fonts is not None and fonts.get(_w("eastAsia")):
        props["font"] = fonts.get(_w("eastAsia"))
    lang = rpr.find(_w("lang"))
    if lang is not None and lang.get(_w("eastAsia")):
        props["lang"] = lang.get(_w("eastAsia"))
    return props


def _chinese_language(xml: bytes, styles: _Styles) -> tuple[bytes, LanguageFix]:
    root = etree.fromstring(xml)
    changed = skipped = 0
    for paragraph in root.iter(_w("p")):
        ppr = paragraph.find(_w("pPr"))
        pstyle = ppr.find(_w("pStyle")) if ppr is not None else None
        paragraph_props = dict(styles.default)
        style_id = pstyle.get(_w("val")) if pstyle is not None else styles.default_paragraph
        paragraph_props.update(styles.chain(style_id))
        for run in paragraph.iter(_w("r")):
            rpr = run.find(_w("rPr"))
            props = dict(paragraph_props)
            rstyle = rpr.find(_w("rStyle")) if rpr is not None else None
            if rstyle is not None:
                props.update(styles.chain(rstyle.get(_w("val"))))
            props.update(_font_and_lang(rpr))
            if props.get("font") not in CHINESE_FONTS or props.get("lang") == CHINESE:
                continue
            text = "".join(node.text or "" for node in run.iter(_w("t")))
            if KANA.search(text):
                skipped += 1  # 日本語の文が中国語の字体で書かれている（言語は変えない）
                continue
            if rpr is None:
                rpr = etree.Element(_w("rPr"))
                run.insert(0, rpr)
            lang = rpr.find(_w("lang"))
            if lang is None:
                lang = etree.Element(_w("lang"))
                after = next(
                    (rpr.find(_w(tag)) for tag in AFTER_LANG if rpr.find(_w(tag)) is not None), None
                )
                if after is not None:
                    after.addprevious(lang)
                else:
                    rpr.append(lang)
            lang.set(_w("eastAsia"), CHINESE)
            changed += 1
    if not changed:
        return xml, LanguageFix(0, skipped)
    data = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    return data, LanguageFix(changed, skipped)


def with_chinese_language(source: Path, destination: Path) -> LanguageFix:
    """source を destination に写し、中国語の字体の run の東アジアの言語を zh-CN にする。

    ひらがな・カタカナを含む run は変えない（日本語の文）。差し込んだ文書（保存するもの）は
    変えない。PDF に変換する直前の一時の写しでだけ使う（ADR-0032 の例外の一覧）。
    """
    with zipfile.ZipFile(source) as archive:
        items = [(item, archive.read(item.filename)) for item in archive.infolist()]
        styles = _Styles(
            archive.read("word/styles.xml") if "word/styles.xml" in archive.namelist() else None
        )
    total = LanguageFix()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as output:
        for item, data in items:
            if TEXT_PARTS.fullmatch(item.filename):
                data, fix = _chinese_language(data, styles)
                total = LanguageFix(
                    total.changed + fix.changed, total.skipped_kana + fix.skipped_kana
                )
            output.writestr(item, data)
    return total
