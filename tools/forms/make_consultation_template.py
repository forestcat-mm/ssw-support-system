"""相談記録書（参考様式第5-4号）の template.docx とマッピング定義の JSON を作る（開発用。T35、ADR-0024）。

original の word/document.xml に docxtpl の差込タグを置くだけで、ほかの zip の中身はそのまま写す。
タグは {{ 名前 or "元の文字" }}（tools/forms/make_contract_templates.py と同じ）。表の 9 行は、番号の
欄（「1」〜「9」）の後ろの 9 つの欄（相談受理日・氏名・性別・国籍・地域・生年月日・在留カード番号・
相談内容・対応結果・対応者の氏名）に置く。出力は手で直さない。標準ライブラリだけで動く。

使い方（リポジトリの直下で）:
    python3 tools/forms/make_consultation_template.py templates/forms/5-4/2026-10-18 \\
        > mappings/5-4/2026-10-18.json
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import docx_tags  # noqa: E402
from make_contract_templates import apply, width  # noqa: E402

VALID_FROM = "2026-10-18"
ROWS = 9
HEADER = (
    "（[from_m:2]月[from_d:2]日から[to_m:2]月[to_d:2]日まで）＿＿＿＿＿＿＿＿＿＿"
    "機関の氏名又は名称：[organization_name:]"
)
CELLS = (
    ("received_on", "相談受理日", "text"),
    ("name", "相談者 氏名", "text"),
    ("sex", "相談者 性別", "text"),
    ("nationality", "相談者 国籍・地域", "text"),
    ("birth_date", "相談者 生年月日", "text"),
    ("card_number", "相談者 在留カード番号", "text"),
    ("content", "相談内容", "text"),
    ("result", "対応結果", "text"),
    ("staff", "対応者の氏名", "text"),
)


def make(document: docx_tags.Document) -> list[dict]:
    items: list[dict] = []
    header = apply(document, [HEADER])
    for slot in header:
        name = slot["name"]
        item = {
            "key": f"c.{name}",
            "target": name,
            "format": "text" if name == "organization_name" else "zen",
            "label": {"organization_name": "機関の氏名又は名称"}.get(name, "対象期間"),
        }
        if slot["original"]:
            item |= {"pad": width(slot["original"]), "align": "right"}
        items.append(item)
    start = 0
    for row in range(1, ROWS + 1):
        number = document.find(str(row), start=start)
        if document.text(number) != str(row):
            raise docx_tags.TagError(f"row {row}: {document.text(number)!r}")
        for offset, (cell, label, fmt) in enumerate(CELLS, 1):
            index = number + offset
            name = f"r{row}_{cell}"
            original = document.text(index)
            if original.strip():
                document.put_tag(index, original, name)
            else:
                document.append_to_empty(index, docx_tags.default_tag(name, ""))
            items.append(
                {"key": f"c.{name}", "target": name, "format": fmt, "label": f"{row} 行目 {label}"}
            )
        start = number + len(CELLS) + 1
    return items


def main(folder: str) -> None:
    source = Path(folder) / "original.docx"
    destination = Path(folder) / "template.docx"
    with zipfile.ZipFile(source) as original:
        document = docx_tags.Document(original.read("word/document.xml").decode("utf-8"))
        items = make(document)
        with zipfile.ZipFile(destination, "w") as out:
            for item in original.infolist():
                if item.filename == "word/document.xml":
                    out.writestr(item, document.xml.encode("utf-8"))
                else:
                    out.writestr(item, original.read(item.filename))
    mapping = {
        "form_id": "5-4",
        "valid_from": VALID_FROM,
        "note": (
            "T35 の全欄（ADR-0024）。受入れ機関×期間の相談の 9 件ごとに 1 つの生成物（T35-D9・D10）。"
            "値は services/form_data_consultation.py が作る。"
        ),
        "fields": items,
        "rules": [],
    }
    print(json.dumps(mapping, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: make_consultation_template.py <version folder>")
    main(sys.argv[1])
