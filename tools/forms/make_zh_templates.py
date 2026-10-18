"""日中併記の様式（第1-5号・第5-8号・第5-9号）の template.docx とマッピング定義を作る（開発用。T38、ADR-0027）。

元は extract_zh_forms.py が入管の中国語の版から取り出した extracted.docx。差込タグを置くだけで、
ほかの zip の中身はそのまま写す。値は日本語の行の欄に入れる（中国語の行の欄は空のまま。T38-D6）。
データのキーは日本語の版と同じ（services/form_data_contract.py・form_data_briefing.py が作る）。
出力は手で直さない（テストで、出力が登録済みのファイルと同じことを確かめる）。標準ライブラリだけで動く。

使い方（リポジトリの直下で）:
    python3 tools/forms/make_zh_templates.py 1-5 templates/forms/1-5/2026-10-18.ja-zh \\
        > mappings/1-5/2026-10-18.ja-zh.json
"""

from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import docx_tags  # noqa: E402
from make_contract_templates import LABELS_1_5, TEXT_FIELDS_1_5, apply, fields_of  # noqa: E402

VALID_FROM = "2026-10-18"
LANGUAGE = "ja-zh"
SP = "　"

# 第1-5号: 日本語の行（句読点は「，」）。甲の記名の欄は、併記の版では 14 字（日本語の版は 18 字）
SPEC_1_5 = [
    "特定技能所属機関[host_name:＿＿＿＿＿＿＿＿＿ ＿＿＿＿＿＿＿＿＿＿＿＿＿]（以下「甲」という。）と",
    "特定技能外国人（候補者を含む。）[worker_name:15]（以下「乙」という。）は，",
    "＿＿＿＿＿＿[signed_y:4]年[signed_m:4]月[signed_d:4]日＿＿締結／签订",
    "甲（甲方）[host_signature:14]盖章…",
]

SLOTS = (
    ("year", 4, "年"),
    ("month", 2, "月"),
    ("day", 2, "日"),
    ("start_hour", 2, "時"),
    ("start_minute", 2, "分から"),
    ("end_hour", 2, "時"),
    ("end_minute", 2, "分まで"),
)


def date_row(row: int, indent: int) -> str:
    return "＿" * indent + "".join(f"[s{row}_{name}:{width}]{unit}" for name, width, unit in SLOTS)


# 第5-8号・第5-9号: 実施日時の日本語の 3 行（1 行目は字下げ 0、2・3 行目は 5 字）
SPEC_BRIEFING = [date_row(1, 0), date_row(2, 5), date_row(3, 5)]
UNDERLINE = re.compile(
    r"<w:r\b[^>]*>(?:(?!</w:r>).)*?<w:u\b[^>]*/>(?:(?!</w:r>).)*?<w:t\b[^>]*>(　+)</w:t>",
    re.S,
)
NAMES = (
    ("の氏名又は名称", "organization_name", "briefing.organization_name",
     "特定技能所属機関（又は登録支援機関）の氏名又は名称"),
    ("説明者の氏名", "explainer_name", "briefing.explainer_name", "説明者の氏名"),
)


def make_briefing(document: docx_tags.Document) -> list[dict]:
    slots = apply(document, SPEC_BRIEFING)
    items = []
    for _anchor, target, key, label in NAMES:
        items.append({"key": key, "target": target, "format": "text", "label": label})
    for slot in slots:
        row, name = re.match(r"s(\d)_(.+)", slot["name"]).groups()  # type: ignore[union-attr]
        items.append(
            {
                "key": f"session{row}.{name}",
                "target": slot["name"],
                "format": "text",
                "label": f"実施日時 {row} 行目",
            }
        )
    # 所属機関名・説明者の氏名: 見出しの後の最初の「下線つきで全角空白だけの run」の頭にタグを置く
    xml = document.xml
    for anchor, target, _key, _label in NAMES:
        match = UNDERLINE.search(xml, xml.index(anchor))
        if match is None:
            raise SystemExit(f"underline after {anchor} not found")
        xml = xml[: match.start(1)] + "{{ " + target + " }}" + xml[match.start(1) :]
    document.xml = xml
    return items


def make(form_id: str, document: docx_tags.Document) -> list[dict]:
    if form_id == "1-5":
        return fields_of("c", apply(document, SPEC_1_5), TEXT_FIELDS_1_5, LABELS_1_5, set())
    return make_briefing(document)


NOTES = {
    "1-5": "T38 の日中併記の版（ADR-0027）。欄とデータのキーは日本語の版（T34、ADR-0023）と同じ。乙の署名は手で書く。",
    "5-8": "T38 の日中併記の版（ADR-0027）。欄とデータのキーは日本語の版（T32、ADR-0021）と同じ。値は日本語の行に入れる。",
    "5-9": "T38 の日中併記の版（ADR-0027）。欄とデータのキーは日本語の版（T32、ADR-0021）と同じ。値は日本語の行に入れる。",
}


def main(form_id: str, folder: str) -> None:
    source = Path(folder) / "extracted.docx"
    with zipfile.ZipFile(source) as original:
        document = docx_tags.Document(original.read("word/document.xml").decode("utf-8"))
        items = make(form_id, document)
        with zipfile.ZipFile(Path(folder) / "template.docx", "w") as out:
            for item in original.infolist():
                if item.filename == "word/document.xml":
                    out.writestr(item, document.xml.encode("utf-8"))
                else:
                    out.writestr(item, original.read(item.filename))
    mapping = {
        "form_id": form_id,
        "valid_from": VALID_FROM,
        "language": LANGUAGE,
        "note": NOTES[form_id],
        "fields": items,
        "rules": [],
    }
    print(json.dumps(mapping, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in NOTES:
        raise SystemExit("usage: make_zh_templates.py <1-5|5-8|5-9> <folder>")
    main(sys.argv[1], sys.argv[2])
