"""申請の書類の一覧（提出書類一覧・確認表）の定義を、入管の Excel から作る（開発用。T37、ADR-0026）。

入管の「特定技能１号」に係る提出書類一覧・確認表（application_sets/sources/*.xlsx）の第1〜3表の行を
すべて取り出し（T37-D2）、行ごとに次を付けて application_sets/<申請の種類>/<適用開始日>.json に出す。
- how: どう満たすか（generate: 生成する様式 ／ attachment: 添付の種類 ／ out: システムの外）
- applies: 当てはまる条件（corporate・individual・industrial・food・full_support・not_full_support・
  variable_one_year・nationality・track_record）。条件の無い行はいつも当てはまる。
出力は手で直さない（テストで、出力が登録済みのファイルと同じことを確かめる）。標準ライブラリだけで動く。

使い方（リポジトリの直下で）:
    python3 tools/forms/make_application_sets.py [出力のフォルダ（既定は application_sets）]
"""

from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import unescape

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "application_sets" / "sources"
VALID_FROM = "2026-10-09"  # 第1表の「令和８年１０月９日改定」
SETS = {
    "coe": ("在留資格認定証明書交付申請", ["coe_t1", "coe_t2_1", "coe_t2_2", "coe_t2_3", "coe_ind", "coe_food"]),
    "chg": ("在留資格変更許可申請", ["chg_t1", "chg_t2_1", "chg_t2_2", "chg_t2_3", "chg_ind", "chg_food"]),
    "ext": ("在留期間更新許可申請", ["ext_t1", "ext_t3"]),
}
SHEET_APPLIES = {"t2_1": "track_record", "t2_2": "corporate", "t2_3": "individual", "ind": "industrial", "food": "food"}
EXT_FIELDS = {"工業製品製造業": "industrial", "飲食料品製造業": "food"}
NATIONALITIES = {"カンボジア": "KH", "タイ": "TH", "ベトナム": "VN", "フィリピン": "PH", "ネパール": "NP",
                 "ミャンマー": "MM", "モンゴル": "MN", "スリランカ": "LK", "インドネシア": "ID",
                 "バングラデシュ": "BD", "ウズベキスタン": "UZ", "パキスタン": "PK", "インド": "IN",
                 "キルギス": "KG", "タジキスタン": "TJ", "マレーシア": "MY", "ラオス": "LA"}
GENERATED = [
    ("第1-17号", "1-17"), ("第1-25号", "1-25"), ("第1-4号", "1-4"), ("第1-5号", "1-5"),
    ("第1-6号別紙", "1-6"), ("第1-6号", "1-6"), ("第3-1号", "3-1"), ("第13-1号", "13-1"),
    ("第13-2号", "13-2"),
]
REF = re.compile(r"([A-Z]+)(\d+)$")


def column(letters: str) -> int:
    value = 0
    for char in letters:
        value = value * 26 + ord(char) - 64
    return value


def cells(path: Path) -> dict[tuple[int, int], str]:
    """最初のシートのセルの文字（共有文字列。ふりがなは除く）。(行, 列) → 文字。"""
    with zipfile.ZipFile(path) as archive:
        shared = archive.read("xl/sharedStrings.xml").decode("utf-8")
        sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
    strings = []
    for item in re.findall(r"<si>(.*?)</si>", shared, re.S):
        item = re.sub(r"<rPh\b.*?</rPh>", "", item, flags=re.S)
        strings.append(unescape("".join(re.findall(r"<t[^>]*>(.*?)</t>", item, re.S))))
    result = {}
    for match in re.finditer(r'<c r="([A-Z]+\d+)"((?:[^>/]|/(?!>))*)>(.*?)</c>', sheet, re.S):
        value = re.search(r"<v>(.*?)</v>", match.group(3), re.S)
        if value is None:
            continue
        text = strings[int(value.group(1))] if 't="s"' in match.group(2) else value.group(1)
        letters, number = REF.match(match.group(1)).groups()  # type: ignore[union-attr]
        result[(int(number), column(letters))] = text.replace("\r\n", "\n").strip()
    return result


def clean(text: str) -> str:
    return re.sub(r"\s*\n\s*", "", text).strip()


def table_rows(key: str) -> list[dict]:
    path = next(SOURCES.glob(f"{key}_*.xlsx"))
    grid = cells(path)
    header_row = next(
        row for (row, col), text in sorted(grid.items()) if text == "必要書類" or clean(text) == "必要書類"
    )
    header = {col: clean(text) for (row, col), text in grid.items() if row == header_row}
    find = lambda prefix: next(col for col, text in header.items() if text.startswith(prefix))  # noqa: E731
    number_col, document_col = find("番号"), find("必要書類")
    form_col, need_col, note_col = find("様式番号"), find("提出"), find("留意事項")
    field_col = next((col for col, text in header.items() if text == "分野"), None)
    title = next(
        (re.search(r"＜(第.+?)＞", text).group(1) for (row, col), text in sorted(grid.items())  # type: ignore[union-attr]
         if row < header_row and "＜第" in text),
        "",
    )
    rows: list[dict] = []
    number = group = condition = field = ""
    for row in sorted({r for r, _c in grid if r > header_row}):
        value = lambda col: grid.get((row, col), "") if col else ""  # noqa: E731
        need = clean(value(need_col))
        if field_col is not None and value(field_col):
            field = clean(value(field_col))
        if value(number_col) and clean(value(number_col)).isdigit():
            if clean(value(number_col)) != number:
                group = condition = ""
            number = clean(value(number_col))
        if need not in ("○", "△"):
            continue
        texts = [value(col) for col in range(document_col, form_col)]
        filled = [(index, text) for index, text in enumerate(texts) if text]
        if len(texts) >= 3:
            if texts[0]:
                group = texts[0]
            if texts[1]:
                condition = texts[1]
            document = texts[-1] or (texts[0] if not texts[1] else "")
            if not texts[-1] and texts[0] and not texts[1]:
                group, document = "", texts[0]
        else:
            document = filled[-1][1] if filled else ""
        if not document.strip():  # 申請情報の記載だけの行（第2表の1の⑤）
            document = clean(value(note_col)).lstrip("※")
        scope = f"{EXT_FIELDS.get(field, field)}-" if field_col is not None else ""
        same = [r for r in rows if r["number"] == number and r.get("field", "") == field]
        rows.append(
            {
                "id": f"{key}-{scope}{number}-{len(same) + 1}",
                "table": title,
                "number": number,
                "group": clean(group),
                "condition": clean(condition),
                "document": document.strip(),
                "form": clean(value(form_col)),
                "need": need,
                "note": value(note_col).strip(),
                **({"field": field} if field_col is not None else {}),
            }
        )
    if field_col is not None:
        rows = [row for row in rows if row.get("field") in EXT_FIELDS]
    return rows


def how(row: dict) -> dict:
    form = row["form"].replace("\n", "")
    for mark, form_id in GENERATED:
        if mark in form:
            return {"generate": form_id}
    document = row["document"]
    if "第1-3号" in form:
        return {"attachment": "health_check"}
    if "構成員であることの証明書（登録支援機関）" in document:
        return {"attachment": "own_council_certificate"}
    if "構成員であることの証明書" in document or "JAIM" in document:
        return {"attachment": "membership_certificate"}
    if "年間カレンダー" in document or "変形労働時間制に関する協定書" in document:
        return {"attachment": "variable_hours_documents"}
    if "賃金規程の写し" in document:
        return {"attachment": "equal_pay_evidence"}
    return {"out": True}


def applies(key: str, row: dict) -> list[str]:
    result = []
    suffix = key.split("_", 1)[1]
    if suffix in SHEET_APPLIES:
        result.append(SHEET_APPLIES[suffix])
    if row.get("field") in EXT_FIELDS:
        result.append(EXT_FIELDS[row["field"]])
    note = row["note"]
    if "全部を登録支援機関に委託しない場合" in note or "全部を委託しない場合" in note:
        result.append("not_full_support")
    elif "全部を登録支援機関に委託する場合" in note or "全部を委託する場合" in note or "全部の実施を委託する場合" in note:
        result.append("full_support")
    if "１年単位の変形労働時間制" in note:
        result.append("variable_one_year")
    if "対象の国籍" in note:
        codes = sorted({code for name, code in NATIONALITIES.items() if name in note})
        result.append("nationality:" + ",".join(codes))
    return result


def main(out: Path) -> None:
    for set_id, (name, keys) in SETS.items():
        rows = []
        for key in keys:
            for row in table_rows(key):
                rows.append(row | {"how": how(row), "applies": applies(key, row)})
        sources = [
            {"file": path.name}
            for key in keys
            for path in sorted(SOURCES.glob(f"{key}_*.xlsx"))
        ]
        data = {
            "id": set_id,
            "name": name,
            "valid_from": VALID_FROM,
            "note": (
                "「特定技能１号」に係る提出書類一覧・確認表の第1〜3表の全部の行（T37-D2、ADR-0026）。"
                "tools/forms/make_application_sets.py が application_sets/sources の Excel から作る。"
            ),
            "sources": sources,
            "rows": rows,
        }
        folder = out / set_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{VALID_FROM}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
        print(set_id, len(rows))


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "application_sets")
