"""第3-1号・第13-1号の誓約事項の産業の一覧（日本標準産業分類）を、様式の本文から取り出してデータにする
（開発用。T37-D4、ADR-0026）。

第3-1号は誓約事項 2（１号の活動を行う事業所。76 の産業）、第13-1号は誓約事項 3（10 の産業）。
「（…を除く。）」の指定は除外のコード、「…に限る」「ただし、…に限る」は限定（システムでは確かめられ
ないので警告）として持つ。出力は手で直さない（テストで、出力が登録済みのファイルと同じことを確かめる）。
標準ライブラリだけで動く。

使い方（リポジトリの直下で）:
    python3 tools/forms/make_industry_lists.py > backend/services/data/industries.json
"""

from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import docx_tags  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SOURCES = {
    # (様式, 一覧の前の誓約事項の頭, 一覧の後の誓約事項の頭)
    "3-1": (ROOT / "templates/forms/3-1/2026-10-18/original.docx", "２\u3000特定技能外国人", "３\u3000特定技能外国人"),
    "13-1": (ROOT / "templates/forms/13-1/2026-10-18/original.docx", "３．", "４．"),
}
ITEM = re.compile(r"^(\d+)[\s　]+(中分類|小分類|細分類)(\d+)[―\s　]*(.+)$")
CODE = re.compile(r"(中分類|小分類|細分類)(\d+)")
LEVELS = {"中分類": 2, "小分類": 3, "細分類": 4}


def texts(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        document = docx_tags.Document(archive.read("word/document.xml").decode("utf-8"))
    return [docx_tags.paragraph_text(p) for p in document.paragraphs()]


def entries(form_id: str) -> list[dict]:
    path, start_mark, end_mark = SOURCES[form_id]
    lines = texts(path)
    start = next(i for i, t in enumerate(lines) if t.startswith(start_mark) and "日本標準産業分類" in t)
    end = next(i for i, t in enumerate(lines) if i > start and t.startswith(end_mark))
    items: list[dict] = []
    for text in lines[start + 1 : end]:
        text = text.strip()
        if not text:
            continue
        match = ITEM.match(text)
        if match is None:  # 前の行の続き（第13-1号の 10）
            items[-1]["name"] += text
            continue
        number, level, code, name = match.groups()
        if LEVELS[level] != len(code) and not (level == "中分類" and len(code) == 2):
            raise SystemExit(f"{form_id} {number}: {level}{code}")
        items.append({"number": int(number), "level": level, "code": code, "name": name.strip()})
    for item in items:
        name = item["name"]
        excluded = re.search(r"（(.*)を除く。）$", name)
        partly = excluded is not None and "のうち" in excluded.group(1)
        codes = [c for _l, c in CODE.findall(excluded.group(1))] if excluded else []
        item["excludes"] = [] if partly else codes
        # 「細分類…のうち…以外の産業を除く」は、そのコードだけが限定つき（システムでは確かめない）
        item["limited_codes"] = codes if partly else []
        item["limited"] = "に限る" in name
    return items


def main() -> None:
    data = {
        "description": (
            "第3-1号の誓約事項 2・第13-1号の誓約事項 3 の産業（日本標準産業分類）。"
            "tools/forms/make_industry_lists.py が様式の本文から作る（手で直さない）。"
            "excludes は除外のコード、limited はシステムでは確かめられない限定（警告）、"
            "limited_codes は一部だけを除くコード（そのコードだけ警告）。"
        ),
        "lists": {form_id: entries(form_id) for form_id in SOURCES},
    }
    print(json.dumps(data, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
