"""事前ガイダンス・生活オリエンテーションの確認書（第5-9号・第5-8号）の template.docx を作る（開発用。
T32、ADR-0021。T29 の make_5_9_template.py を一般化した）。

original の word/document.xml に docxtpl の差込タグを置くだけで、ほかの zip の中身はそのまま写す。
出力は手で直していない（直したら ADR-0021 に書く）。標準ライブラリだけで動く。

- 実施日時の 3 行: 欄の空白をタグで置き換える（記載例どおり値が欄の位置に入る。T32-D2）。空のときは
  データの側が元の空白を入れる。2・3 行目は、行頭の空白の run を 6 字から 5 字にし、年の欄（4 字）を
  タグの側に移す（空のとき元と同じ 9 字の空白になる）。
- 所属機関名・説明者の氏名: 見出しの後の最初の「下線つきで全角空白だけの run」の頭にタグを置く。

使い方（リポジトリの直下で）:
    python3 tools/forms/make_briefing_template.py 5-9 \\
        templates/forms/5-9/2026-10-17/original.docx templates/forms/5-9/2026-10-17/template.docx
    python3 tools/forms/make_briefing_template.py 5-8 \\
        templates/forms/5-8/2026-10-17/original.docx templates/forms/5-8/2026-10-17/template.docx
"""

import re
import sys
import zipfile

SP = "　"
SLOTS = (("year", 4, "年"), ("month", 2, "月"), ("day", 2, "日"), ("start_hour", 2, "時"),
         ("start_minute", 2, "分から"), ("end_hour", 2, "時"), ("end_minute", 2, "分まで"))


def tag(name: str) -> str:
    return "{{ " + name + " }}"


def row_text(row: int) -> str:
    return "".join(tag(f"s{row}_{name}") + unit for name, _width, unit in SLOTS)


def replace_once(xml: str, old: str, new: str) -> str:
    if old not in xml:
        raise SystemExit(f"not found: {old!r}")
    return xml.replace(old, new, 1)


def add_tags(xml: str) -> str:
    within = r"((?:(?!</w:p>).)*?)"  # 同じ段落の中だけ
    # 1 行目（1 つの run）
    first = "".join(SP * width + unit for _name, width, unit in SLOTS)
    xml = replace_once(xml, f">{first}</w:t>", f">{row_text(1)}</w:t>")
    # 2・3 行目（run が [全角空白 6][全角空白 2][全角空白 1 + 年…分まで] に分かれている）
    rest = SP + "年" + "".join(SP * width + unit for _name, width, unit in SLOTS[1:])
    pattern = re.compile(
        rf">{SP * 6}</w:t>{within}>{SP * 2}</w:t>{within}>{re.escape(rest)}</w:t>", re.S
    )
    for row in (2, 3):
        match = pattern.search(xml)
        if match is None:
            raise SystemExit(f"date row {row} not found")
        new = f">{SP * 5}</w:t>{match.group(1)}></w:t>{match.group(2)}>{row_text(row)}</w:t>"
        xml = xml[: match.start()] + new + xml[match.end() :]
    # 所属機関名・説明者の氏名（見出しの後の最初の、下線つきで全角空白だけの run）
    run = re.compile(
        r"<w:r\b[^>]*>(?:(?!</w:r>).)*?<w:u\b[^>]*/>(?:(?!</w:r>).)*?<w:t\b[^>]*>(\u3000+)</w:t>",
        re.S,
    )
    for anchor, name in (("の氏名又は名称", "organization_name"), ("説明者の氏名", "explainer_name")):
        match = run.search(xml, xml.index(anchor))
        if match is None:
            raise SystemExit(f"underline after {anchor} not found")
        start = match.start(1)
        xml = xml[:start] + tag(name) + xml[start:]
    return xml


def main(form_id: str, source: str, destination: str) -> None:
    if form_id not in ("5-8", "5-9"):
        raise SystemExit("form_id must be 5-8 or 5-9")
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(destination, "w") as out:
        xml = add_tags(original.read("word/document.xml").decode("utf-8"))
        for item in original.infolist():
            if item.filename == "word/document.xml":
                out.writestr(item, xml.encode("utf-8"))
            else:
                out.writestr(item, original.read(item.filename))


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: make_briefing_template.py <5-8|5-9> <original.docx> <template.docx>")
    main(sys.argv[1], sys.argv[2], sys.argv[3])
