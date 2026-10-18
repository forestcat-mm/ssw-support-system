"""第5-9号（事前ガイダンスの確認書）の template.docx を original.docx から作る（開発用。ADR-0018）。

original の word/document.xml の 3 か所に docxtpl の差込タグを足すだけで、ほかの zip の中身は
そのまま写す。出力は手で直していない（直したら ADR-0018 の「template.docx の作り方」に書く）。

使い方（リポジトリの直下で。標準ライブラリだけで動く）:
    python3 tools/forms/make_5_9_template.py \\
        templates/forms/5-9/2026-10-17/original.docx \\
        templates/forms/5-9/2026-10-17/template.docx
作った後は manifest.json の template_sha256 を出力の SHA-256 に合わせ、make forms で読み込む。
"""

import sys
import zipfile

SPACE = "　"  # 全角空白（様式の下線の欄は全角空白の run）


def replace_once(xml: str, old: str, new: str) -> str:
    if old not in xml:
        raise SystemExit(f"not found: {old!r}")
    return xml.replace(old, new, 1)


def tag(name: str) -> str:
    return "{{ " + name + " }}"


def add_tags(xml: str) -> str:
    s = SPACE
    # 実施日時の 1 行目（1 つの run）
    line = f"{s * 4}年{s * 2}月{s * 2}日{s * 2}時{s * 2}分から{s * 2}時{s * 2}分まで"
    tagged = (
        f"{s * 4}{tag('s1_year')}年{s * 2}{tag('s1_month')}月{s * 2}{tag('s1_day')}日{s * 2}"
        f"{tag('s1_start_hour')}時{s * 2}{tag('s1_start_minute')}分から{s * 2}"
        f"{tag('s1_end_hour')}時{s * 2}{tag('s1_end_minute')}分まで"
    )
    xml = replace_once(xml, f">{line}</w:t>", f">{tagged}</w:t>")
    # 見出しの後の最初の下線の run の頭にタグを置く（所属機関名は全角空白 19、説明者は 16）
    for anchor, spaces, name in (
        ("の氏名又は名称", 19, "organization_name"),
        ("説明者の氏名", 16, "explainer_name"),
    ):
        at = xml.index(anchor)
        blank = f">{s * spaces}</w:t>"
        xml = xml[:at] + replace_once(xml[at:], blank, f">{tag(name)}{s * spaces}</w:t>")
    return xml


def main(source: str, destination: str) -> None:
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(destination, "w") as out:
        xml = add_tags(original.read("word/document.xml").decode("utf-8"))
        for item in original.infolist():
            if item.filename == "word/document.xml":
                out.writestr(item, xml.encode("utf-8"))
            else:
                out.writestr(item, original.read(item.filename))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: make_5_9_template.py <original.docx> <template.docx>")
    main(sys.argv[1], sys.argv[2])
