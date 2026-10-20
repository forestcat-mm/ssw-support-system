"""Word の様式に差し込む（docxtpl。T29、ADR-0018）。

template.docx は、入管の original の写しに差込タグ（{{ 名前 }}）を置くことだけをしたもの。
タグを外した本文が original の本文と同じことをテストで確かめる（tests/forms_engine）。
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Mapping
from pathlib import Path

from docxtpl import DocxTemplate  # type: ignore[import-untyped]

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
