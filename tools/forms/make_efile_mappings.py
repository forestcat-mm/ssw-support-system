"""電子届出の一括申込用ツールの欄の対応（データのキー → 列）を作る（T50。ADR-0040）。

列の表（extract_efile_columns.py の出力 mappings/efile-*/<適用開始日>.columns.json）の項目名から、
本人の欄（英字氏名・性別・国籍・地域・生年月日・在留カード番号・特定産業分野・業務区分）を当て、
届出の内容の欄は下の EVENTS の表（列 → キー・ラベル・見出しの字数）で当てる。
出力は mappings/efile-*/<適用開始日>.json。手で直さない（規則の .rules.json は空。検査は列の表で行う）。

使い方（リポジトリの直下で）:
  python tools/forms/make_efile_mappings.py efile-3-1 2026-04-01 > mappings/efile-3-1/2026-04-01.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FORMS = (
    "efile-3-1",
    "efile-3-2",
    "efile-3-3",
    "efile-3-4",
    "efile-3-5",
    "efile-3-7",
    "efile-4-3",
)
PERSON = {
    "英字氏名": ("p.name", "英字氏名"),
    "性別": ("p.sex", "性別"),
    "国籍・地域": ("p.nationality", "国籍・地域"),
    "生年月日": ("p.birth_date", "生年月日"),
    "在留カード番号": ("p.card", "在留カード番号"),
    "特定産業分野": ("p.field", "特定産業分野"),
    "業務区分": ("p.category", "業務区分"),
}
# 列 → (キー, ラベル, 見出しの字数〔全角 n 文字以内。無ければ None〕)
SUPPORT_DIFFICULTY = {
    "B": ("e.reason", "事由", None),
    "C": ("e.category", "事由の区分", None),
    "D": ("e.category_other", "事由の区分 その他内容", 30),
    "E,G,I": ("e.date", "事由発生年月日", None),
    "K": ("e.summary", "事案の概要", 20),
    "L": ("e.measure", "措置内容", None),
    "M": ("e.measure_reason", "措置内容 理由", 20),
}
EVENTS: dict[str, dict[str, tuple[str, str, int | None]]] = {
    "efile-3-1": {
        "B": ("e.reason", "届出の事由", None),
        "C,E,G": ("e.date", "届出の事由発生年月日", None),
        "I": ("e.change_major", "変更事項 大分類", None),
        "J": ("e.change_minor", "変更事項 小分類", None),
        "K": ("e.change_content", "変更後の内容", 20),
        "L": ("e.end_reason", "終了の事由", None),
        "M": ("e.end_detail", "終了の事由 事由の詳細", None),
        "N": ("e.end_other", "終了の事由 その他の理由", 20),
        "O": ("e.new_content", "新たな雇用契約の締結 契約の内容", None),
    },
    "efile-3-2": {
        "B,D,F": ("e.date", "変更年月日", None),
        "H": ("e.change_major", "大分類", None),
        "I": ("e.change_minor", "中分類", None),
        "J": ("e.change_content", "変更後の内容", 20),
    },
    "efile-3-3": {
        "B": ("e.reason", "届出の事由", None),
        "C,E,G": ("e.date", "届出の事由発生年月日", None),
        "I": ("e.conclusion_content", "Ａ 契約の締結 契約の内容", 20),
        "J": ("e.change_item", "Ｂ 変更の内容 変更事項", None),
        "K": ("e.change_content", "Ｂ 変更の内容 変更後の内容", 20),
        "L": ("e.end_major", "Ｃ 契約の終了 終了の事由 大分類", None),
        "M": ("e.end_minor", "Ｃ 契約の終了 終了の事由 小分類", None),
        "N": ("e.end_other", "Ｃ 契約の終了 終了の事由 その他の理由", 20),
        "AA": ("s.registration_prefix", "登録支援機関 登録番号（前２桁）", None),
        "AC": ("s.registration_number", "登録支援機関 登録番号（後６桁）", None),
        "AD": ("s.corporate_number", "登録支援機関 法人番号", None),
        "AE": ("s.name", "登録支援機関 機関の氏名又は名称", None),
        "AF": ("s.postal_code", "登録支援機関 郵便番号", None),
        "AG": ("s.prefecture", "登録支援機関 都道府県", None),
        "AH": ("s.city", "登録支援機関 市区町村", None),
        "AI": ("s.street", "登録支援機関 町名丁目番地号等", None),
    },
    "efile-3-4": {
        "B": ("e.reason", "届出の事由", None),
        "C": ("e.category", "事由の区分", None),
        "D": ("e.category_other", "事由の区分 その他内容", 30),
        "E,G,I": ("e.date", "届出の事由発生年月日", None),
        "K": ("e.summary", "事案の概要", 20),
        "L": ("e.state", "特定技能外国人の現状", None),
        "M": ("e.intention", "受入れ継続のための措置 活動継続の意思", None),
        "N": ("e.measure", "受入れ継続のための措置 措置内容", None),
    },
    "efile-3-5": {
        "B,D,F": ("e.known_on", "基準不適合を知った日", None),
        "H,J,L": ("e.occurred_on", "基準不適合が発生した日", None),
        "N": ("e.content", "基準不適合の内容", None),
        "O": ("e.trigger", "基準不適合を知った経緯・内容 端緒", None),
        "P": ("e.trigger_other", "端緒 その他内容", None),
        "Q": ("e.detail", "基準不適合の具体的な内容", 30),
        "R": ("e.response", "基準不適合への対応 対応区分", None),
        "S": ("e.result", "基準不適合への対応 対応結果", 30),
    },
    "efile-3-7": SUPPORT_DIFFICULTY,
    "efile-4-3": SUPPORT_DIFFICULTY,
}
FILERS = {"efile-4-3": "organization"}
NOTES = {
    "efile-4-3": "報告機関は自機関（登録支援機関）。",
}


def make(form_id: str, valid_from: str, root: Path) -> dict:
    table = json.loads(
        (root / form_id / f"{valid_from}.columns.json").read_text("utf-8")
    )
    columns = {column["column"]: column for column in table["columns"]}
    fields = []
    for column in table["columns"]:
        if column["name"] in PERSON:
            key, label = PERSON[column["name"]]
            fields.append(_field(key, column["column"], label, None, columns, table))
    for target, (key, label, advice) in EVENTS[form_id].items():
        if target not in columns and target not in table["lists"]:
            raise SystemExit(f"{form_id}: {target} is not in the tool")
        fields.append(_field(key, target, label, advice, columns, table))
    for key, cell in table["organization_cells"].items():
        fields.append(
            {
                "key": f"org.{key}",
                "target": f"org:{key}",
                "format": "text",
                "label": {
                    "corporate_number": "法人番号",
                    "name": "名称",
                    "address": "住所",
                }[key],
            }
        )
    mapped = {field["target"] for field in fields}
    missing = [
        column["column"]
        for column in table["columns"]
        if column["column"] not in mapped
    ]
    if missing:
        raise SystemExit(f"{form_id}: columns without a key: {missing}")
    return {
        "form_id": form_id,
        "valid_from": valid_from,
        "note": "電子届出の一括申込用ツールの欄の対応（T50、ADR-0040）。"
        "tools/forms/make_efile_mappings.py が作る。値は services/efile_data.py が作る。"
        + NOTES.get(form_id, "届出機関は受入れ機関。"),
        "filer": FILERS.get(form_id, "host"),
        "fields": fields,
    }


def _field(
    key: str,
    target: str,
    label: str,
    advice: int | None,
    columns: dict[str, dict],
    table: dict,
) -> dict:
    is_date = "," in target
    field: dict = {
        "key": key,
        "target": target,
        "format": "date3" if is_date else "text",
        "label": label,
    }
    if target in table["lists"]:
        field["choices"] = True
    if advice:
        field["advice_length"] = advice
    return field


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    form_id, valid_from = sys.argv[1], sys.argv[2]
    print(json.dumps(make(form_id, valid_from, ROOT / "mappings"), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
