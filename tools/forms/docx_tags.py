"""Word の様式の word/document.xml に docxtpl の差込タグを置く道具の共通部分（開発用。T34、ADR-0023）。

標準ライブラリだけで動く。段落は本文の順に数える（表の中も含む。番号は 0 から）。段落の中の文字は
run の <w:t> をつないだもので探し、見つけた文字の範囲を、最初の <w:t> にタグの文として置き、
残りの <w:t> からは除く（run の書式は最初の run のものになる）。

タグは {{ 名前 or "元の文字" }} の形にする（default_tag）。値が無いときは元の文字（空白や □）に戻るので、
値を何も入れずに差し込んだ文書の本文は original と同じになる（tests/forms_engine で確かめる）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PARAGRAPH = re.compile(r"<w:p\b(?:(?!<w:p\b).)*?</w:p>", re.S)
TEXT = re.compile(r"(<w:t(?:\s[^>]*[^/])?>)(.*?)(</w:t>)", re.S)


class TagError(SystemExit):
    """original の形が想定と違う（様式が変わった）。"""


def default_tag(name: str, original: str) -> str:
    """値が無いときは元の文字に戻るタグ。"""
    if '"' in original or "\\" in original:
        raise TagError(f"cannot quote {original!r}")
    return "{{ " + name + ' or "' + original + '" }}'


def plain_tag(name: str) -> str:
    return "{{ " + name + " }}"


def paragraph_spans(xml: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in PARAGRAPH.finditer(xml)]


def paragraph_text(paragraph: str) -> str:
    return "".join(m.group(2) for m in TEXT.finditer(paragraph))


@dataclass
class Document:
    """document.xml の文字列と、段落の位置（置き換えのたびに数え直す）。"""

    xml: str

    def paragraphs(self) -> list[str]:
        return [self.xml[start:end] for start, end in paragraph_spans(self.xml)]

    def text(self, index: int) -> str:
        return paragraph_text(self.paragraphs()[index])

    def find(self, contains: str, occurrence: int = 0, start: int = 0) -> int:
        """contains を含む occurrence 番目（0 から）の段落の番号（start 番から探す）。"""
        found = [
            i for i, p in enumerate(self.paragraphs()) if i >= start and contains in paragraph_text(p)
        ]
        if len(found) <= occurrence:
            raise TagError(f"paragraph with {contains!r} (#{occurrence}) not found")
        return found[occurrence]

    def _replace_paragraph(self, index: int, new: str) -> None:
        start, end = paragraph_spans(self.xml)[index]
        self.xml = self.xml[:start] + new + self.xml[end:]

    def put(self, index: int, old: str, new: str, occurrence: int = 0) -> None:
        """段落 index の文字 old（occurrence 番目）を new にする。"""
        paragraph = self.paragraphs()[index]
        texts = list(TEXT.finditer(paragraph))
        joined = "".join(m.group(2) for m in texts)
        position = -1
        for _ in range(occurrence + 1):
            position = joined.find(old, position + 1)
            if position < 0:
                raise TagError(f"{old!r} (#{occurrence}) not found in paragraph {index}: {joined!r}")
        self.put_range(index, position, position + len(old), new)

    def put_range(self, index: int, position: int, end: int, new: str) -> None:
        """段落 index の文字の position〜end（<w:t> をつないだ文字の位置）を new にする。"""
        paragraph = self.paragraphs()[index]
        texts = list(TEXT.finditer(paragraph))
        pieces: list[str] = []
        cursor = 0  # paragraph の中の位置
        offset = 0  # joined の中の位置
        placed = False
        for match in texts:
            content = match.group(2)
            low, high = offset, offset + len(content)
            offset = high
            if position == end:
                if not (low <= position <= high) or placed:
                    continue
            elif high <= position or low >= end:
                continue
            keep_before = content[: max(0, position - low)]
            keep_after = content[max(0, end - low) :] if end < high else ""
            body = keep_before + ("" if placed else new) + keep_after
            placed = True
            opening = match.group(1)
            if 'xml:space="preserve"' not in opening:
                opening = opening[:-1] + ' xml:space="preserve">'
            pieces.append(paragraph[cursor : match.start()])
            pieces.append(opening + body + match.group(3))
            cursor = match.end()
        pieces.append(paragraph[cursor:])
        self._replace_paragraph(index, "".join(pieces))

    def put_tag(
        self, index: int, old: str, name: str, occurrence: int = 0, *, keep: bool = True
    ) -> None:
        """old を、値が無いときは old に戻るタグにする（keep=False なら値が無いと何も出ない）。"""
        self.put(index, old, default_tag(name, old) if keep else plain_tag(name), occurrence)

    def append_to_empty(self, index: int, text: str) -> None:
        """文字の無い段落に、段落の記号の書式（pPr の rPr）で run を足してタグを置く。"""
        paragraph = self.paragraphs()[index]
        if paragraph_text(paragraph).strip():
            raise TagError(f"paragraph {index} is not empty: {paragraph_text(paragraph)!r}")
        run_props = ""
        mark = re.search(r"<w:pPr>.*?(<w:rPr>.*?</w:rPr>).*?</w:pPr>", paragraph, re.S)
        if mark:
            run_props = mark.group(1)
        run = f'<w:r>{run_props}<w:t xml:space="preserve">{text}</w:t></w:r>'
        new = paragraph.replace("</w:p>", run + "</w:p>")
        self._replace_paragraph(index, new)

    def insert_paragraph_before(self, index: int, text: str) -> None:
        """タグだけの段落を index の段落の前に足す（docxtpl の {%p … %} 用。生成では消える）。"""
        start, _end = paragraph_spans(self.xml)[index]
        paragraph = f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'
        self.xml = self.xml[:start] + paragraph + self.xml[start:]

    def insert_paragraph_after_position(self, position: int, text: str) -> None:
        paragraph = f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'
        self.xml = self.xml[:position] + paragraph + self.xml[position:]
