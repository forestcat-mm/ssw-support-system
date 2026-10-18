"""受入れ機関・分野の書類（第3-1号・第13-1号・第13-2号・第5-10号・第1-25号）の template.docx と
マッピング定義の JSON を作る（開発用。T37、ADR-0026）。

tools/forms/make_contract_templates.py と同じ書き方（段落の文字を「＿」と [名前:元の文字] で書いた
SPEC から、original の段落と照らしてタグを置く。タグは値が無いと元の文字に戻る）。第1-25号は配布が
.doc なので、変換した converted.docx を元にする（T34-D1 と同じ）。出力は手で直さない。
標準ライブラリだけで動く。

使い方（リポジトリの直下で）:
    python3 tools/forms/make_application_templates.py 3-1 templates/forms/3-1/2026-10-18 \\
        > mappings/3-1/2026-10-18.json
    （13-1・13-2・5-10・1-25 も同じ）
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import docx_tags  # noqa: E402
from make_contract_templates import SP, apply, width  # noqa: E402

VALID_FROM = "2026-10-18"
FEE_ROWS = 10

PERSON = [  # 所属機関・外国人（第3-1号・第13-1号・第13-2号で同じ）
    "氏名又は名称[host_name:]",
    "住＿＿＿＿所[host_address:]",
    "氏＿＿＿＿名[worker_name:]",
    "性＿＿＿＿別[worker_sex:]",
    "国籍・地域[worker_nationality:]",
    "生年月日[worker_birth_date:]",
]
SPECS: dict[str, list[str]] = {
    "3-1": [
        *PERSON,
        "作成年月日＿＿＿＿[made_y:4]年[made_m:2]月[made_d:2]日",
        "作成責任者＿[author:13]",
    ],
    "13-1": [
        *PERSON,
        "作成年月日＿＿＿[made_y:4]年[made_m:2]月[made_d:2]日",
        "作成責任者＿[author:16]",
    ],
    "13-2": [
        "＿＿＿＿氏名又は名称[organization_name:]",
        "＿＿＿＿住＿＿＿＿所[organization_address:]",
        *PERSON,
        "作成年月日＿[made_y:4]年[made_m:2]月[made_d:2]日",
        "作成責任者＿[author:13]",
    ],
    "5-10": [
        "特定技能所属機関[host_name:22]（以下「甲」という。）は、登録支援機関[organization_name:19]"
        "（以下「乙」という。）に、甲が雇用する１号特定技能外国人[worker_name:20]（以下「丙」という。）"
        "に対する１号特定技能外国人支援計画について、以下のとおり支援業務委託契約を締結する。",
        "…甲が属する[field:●●]分野を所管する関係行政機関の長が基準を定める告示の規定に基づいて"
        "定められた支援を実施すること。",
        "月額[monthly_fee:12]円を支払う。",
        "＿１＿本契約の期間は、[starts_y:4]年[starts_m:2]月[starts_d:2]日から[ends_y:4]年[ends_m:2]月"
        "[ends_d:2]日までとする。…",
        "＿本契約に関する一切の争訟は[court:3]地方裁判所を第一審の専属的合意管轄裁判所とする。",
        "[concluded_y:4]年[concluded_m:2]月[concluded_d:2]日＿＿締結",
        "（甲）[host_signature:18]㊞（乙）[organization_signature:18]㊞＿",
    ],
    "1-25": [
        "（[registration_year:2]登－[registration_serial:5]）",
        "[concluded_y:]年[concluded_m:3]月[concluded_d:3]日",
        "[full_support:該当＿・＿非該当]",
        "[starts_y:2]年[starts_m:2]月[starts_d:2]日から[ends_y:4]年[ends_m:2]月[ends_d:2]日まで",
        "[made_y:]年[made_m:5]月[made_d:5]日",
        "特定技能所属機関の氏名又は名称[host_name:]",
    ],
}
# 中身の無い段落（見出しの段落の次）に置くタグ
EMPTY_AFTER = {
    "1-25": [
        ("申請人（支援対象者）", "worker_name"),
        ("契約の相手方（登録支援機関）", "organization_name"),
        ("委託料（１名当たりの月額）", "monthly_fee_text"),
    ],
}
TEXT = {
    "host_name", "host_address", "worker_name", "worker_sex", "worker_nationality",
    "worker_birth_date", "author", "organization_name", "organization_address", "field",
    "court", "host_signature", "organization_signature", "full_support",
}
ZEN_TEXT = {"monthly_fee_text", "registration_serial"}
YEN = {"monthly_fee"}
LABELS = {
    "host_name": "特定技能所属機関の氏名又は名称",
    "host_address": "特定技能所属機関の住所",
    "worker_name": "特定技能外国人の氏名",
    "worker_sex": "特定技能外国人の性別",
    "worker_nationality": "特定技能外国人の国籍・地域",
    "worker_birth_date": "特定技能外国人の生年月日",
    "made": "作成年月日",
    "author": "作成責任者",
    "organization_name": "登録支援機関の氏名又は名称",
    "organization_address": "登録支援機関の住所",
    "field": "分野（第1条の 11）",
    "monthly_fee": "委託料（月額）",
    "monthly_fee_text": "委託料（１名当たりの月額）",
    "starts": "契約期間（始期）",
    "ends": "契約期間（終期）",
    "court": "専属的合意管轄裁判所",
    "concluded": "締結日（契約年月日）",
    "host_signature": "甲（特定技能所属機関）",
    "organization_signature": "乙（登録支援機関）",
    "registration_year": "登録番号（年）",
    "registration_serial": "登録番号（番号）",
    "full_support": "委託する支援業務（全部であること）",
    "fee": "別紙 委託料の内訳",
    "fee_total": "別紙 合計",
}


def label_of(name: str) -> str:
    for suffix, word in (("_y", "（年）"), ("_m", "（月）"), ("_d", "（日）")):
        if name.endswith(suffix) and name[: -len(suffix)] in LABELS:
            return LABELS[name[: -len(suffix)]] + word
    if name.startswith("fee") and name[3:4].isdigit():
        row, _, part = name[3:].partition("_")
        words = {"name": "名目", "amount": "金額", "occasional": "随時", "regular": "定期"}
        return f"別紙 {row} 行目 {words[part]}"
    return LABELS[name]


def fields_of(slots: list[dict]) -> list[dict]:
    items = []
    for slot in slots:
        name, original = slot["name"], slot["original"]
        if original == "□":
            fmt, align = "check", ""
        elif name in ZEN_TEXT:
            fmt, align = "zen_text", "left"
        elif name in TEXT or name.endswith("_name"):
            fmt, align = "text", "left"
        elif name in YEN or name.endswith("_amount") or name == "fee_total":
            fmt, align = "zen_yen", "right"
        else:
            fmt, align = "zen", "right"
        item = {"key": f"a.{name}", "target": name, "format": fmt, "label": label_of(name)}
        if fmt != "check" and original and not original.strip(SP):
            item |= {"pad": width(original), "align": align}
        items.append(item)
    return items


def fee_rows(document: docx_tags.Document) -> list[dict]:
    """第5-10号 別紙の 10 行（番号の段落の次が名目、その後に金額と徴収時期）。"""
    slots: list[dict] = []
    start = document.find("参考様式第５－１０号・別紙")
    for row in range(1, FEE_ROWS + 1):
        number = document.find(str(row), start=start)
        if document.text(number) != str(row):
            raise docx_tags.TagError(f"fee row {row}")
        document.append_to_empty(number + 1, docx_tags.default_tag(f"fee{row}_name", ""))
        slots.append({"name": f"fee{row}_name", "original": ""})
        amount = document.find("金" + SP * 2 + "額：", start=number)
        slots += _apply_at(document, amount, f"金＿＿額：[fee{row}_amount:13]円")
        timing = document.find("徴収時期：", start=number)
        slots += _apply_at(
            document,
            timing,
            f"徴収時期：[fee{row}_occasional:□]＿随時＿＿＿＿＿[fee{row}_regular:□]＿定期",
        )
        start = timing + 1
    total = document.find("合計", start=start)
    slots += _apply_at(document, total, "合計＿＿＿＿ [fee_total:10]円")
    return slots


def _apply_at(document: docx_tags.Document, index: int, markup: str) -> list[dict]:
    """段落 index に SPEC の 1 行を当てる（同じ文の段落が並ぶ表の行用）。"""
    return apply(document, [markup], start=index)


def author_1_25(document: docx_tags.Document) -> list[dict]:
    """第1-25号の作成責任者（「役職・氏名」の後ろの空白をすべて欄にする）。"""
    index = document.find("作成責任者")
    text = document.text(index)
    position = text.index("役職・氏名") + len("役職・氏名")
    original = text[position:]
    document.put_range(index, position, len(text), docx_tags.default_tag("author", original))
    return [{"name": "author", "original": original}]


def make(form_id: str, document: docx_tags.Document) -> list[dict]:
    slots: list[dict] = []
    start = 0
    for heading, name in EMPTY_AFTER.get(form_id, []):
        index = document.find(heading, start=start)
        document.append_to_empty(index + 1, docx_tags.default_tag(name, ""))
        slots.append({"name": name, "original": ""})
        start = index + 1
    slots += apply(document, SPECS[form_id])
    if form_id == "5-10":
        slots += fee_rows(document)
    if form_id == "1-25":
        slots += author_1_25(document)
    return fields_of(slots)


SOURCES = {"1-25": "converted.docx"}


def main(form_id: str, folder: str) -> None:
    source = Path(folder) / SOURCES.get(form_id, "original.docx")
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
        "note": f"T37 の全欄（ADR-0026）。値は services/form_data_application.py が作る。",
        "fields": items,
        "rules": [],
    }
    print(json.dumps(mapping, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in SPECS:
        raise SystemExit("usage: make_application_templates.py <3-1|13-1|13-2|5-10|1-25> <folder>")
    main(sys.argv[1], sys.argv[2])
