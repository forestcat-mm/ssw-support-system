"""入管の中国語の併記の様式（1 つの Word に 10 の様式）から、様式ごとの部分を取り出す（開発用。T38、ADR-0027）。

元は https://www.moj.go.jp/isa/content/001339744.docx（「英語及び９か国語による様式について」の中国語）。
本文（word/document.xml の w:body）の直下の要素を、様式の見出し（「参考様式第１－５号」など）から次の
様式の見出しの前まで取り出し、ほかの zip の中身はそのまま写す（XML の文字は変えない）。

- 取り出した範囲の最後の節の設定（用紙・余白）は、範囲の最後の要素以後で最初に現れる節の設定を使う。
  それが範囲の最後の段落の中にあれば、段落から外して本文の最後に置く（空の節が増えないように）。
- 範囲の途中の節の区切りはそのまま残す。
- 見出しの段落の頭の改ページ（文字より前の `<w:br w:type="page"/>`）は外す（空の頁ができるため）。
  変える XML はこの 1 か所だけ。
出力は手で直さない（テストで、出力が登録済みのファイルと同じことを確かめる）。標準ライブラリだけで動く。

使い方（リポジトリの直下で）:
    python3 tools/forms/extract_zh_forms.py <元の docx> <様式> <出力の docx>
"""

from __future__ import annotations

import re
import sys
import zipfile

# 様式 → (見出しの頭, 次の様式の見出しの頭。None は文書の終わり)
FORMS = {
    "1-5": ("参考様式第１－５号", "参考様式第１－６号"),
    "1-6": ("参考様式第１－６号", "参考様式第１－１０号"),
    "1-17": ("参考様式第１－１７号", "参考様式第５－７号"),
    "5-8": ("参考様式第５－８号", "参考様式第５－９号"),
    "5-9": ("参考様式第５－９号", None),
}
TAG = re.compile(r"<(/?)(w:[A-Za-z]+)\b[^>]*?(/?)>")
TEXT = re.compile(r"<w:t(?:\s[^>]*[^/])?>([^<]*)</w:t>")
SECTION = re.compile(r"<w:sectPr\b.*?</w:sectPr>", re.S)
PAGE_BREAK = '<w:br w:type="page"/>'


def children(xml: str) -> tuple[int, int, list[tuple[int, int, str]]]:
    """w:body の直下の要素の (開始, 終了, 名前)。"""
    body_start = xml.index("<w:body>") + len("<w:body>")
    body_end = xml.index("</w:body>")
    found: list[tuple[int, int, str]] = []
    depth = 0
    start = 0
    name = ""
    for match in TAG.finditer(xml, body_start, body_end):
        closing, tag, empty = match.groups()
        if depth == 0 and not closing:
            start, name = match.start(), tag
            if empty:
                found.append((start, match.end(), name))
                continue
            depth = 1
        elif not closing and not empty:
            depth += 1
        elif closing:
            depth -= 1
            if depth == 0:
                found.append((start, match.end(), name))
    return body_start, body_end, found


def text_of(fragment: str) -> str:
    return "".join(TEXT.findall(fragment))


def extract(xml: str, form_id: str) -> str:
    head, next_head = FORMS[form_id]
    body_start, body_end, items = children(xml)
    texts = [text_of(xml[s:e]).strip() for s, e, _n in items]
    first = next(i for i, t in enumerate(texts) if t.startswith(head))
    last = (
        next(i for i, t in enumerate(texts) if i > first and t.startswith(next_head))
        if next_head
        else len(items)
    )
    while items[last - 1][2] == "w:sectPr":  # 本文の最後の節の設定は範囲に入れない
        last -= 1
    parts = [xml[s:e] for s, e, _n in items[first:last]]
    # 見出しの段落の頭の改ページ（前の様式の終わり）は外す（空の頁ができる）
    head_break = parts[0].find(PAGE_BREAK)
    if head_break != -1 and not TEXT.search(parts[0], 0, head_break):
        parts[0] = parts[0][:head_break] + parts[0][head_break + len(PAGE_BREAK) :]
    # 範囲の最後の節の設定
    section = None
    for index in range(last - 1, len(items)):
        found = SECTION.search(xml, items[index][0], items[index][1])
        if found:
            section = found.group(0)
            if index == last - 1 and items[index][2] == "w:p":
                parts[-1] = parts[-1].replace(section, "", 1)
            break
    if section is None:
        raise SystemExit("section properties not found")
    return xml[:body_start] + "".join(parts) + section + xml[body_end:]


def main(source: str, form_id: str, destination: str) -> None:
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(destination, "w") as out:
        xml = extract(original.read("word/document.xml").decode("utf-8"), form_id)
        for item in original.infolist():
            if item.filename == "word/document.xml":
                out.writestr(item, xml.encode("utf-8"))
            else:
                out.writestr(item, original.read(item.filename))


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[2] not in FORMS:
        raise SystemExit("usage: extract_zh_forms.py <source.docx> <1-5|1-6|1-17|5-8|5-9> <out.docx>")
    main(sys.argv[1], sys.argv[2], sys.argv[3])
