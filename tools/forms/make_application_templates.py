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
# 第1-26号・第1-11-1号の取得日（改正日の記載なし）
VALID_FROMS = {"1-26": "2026-10-20", "1-11-1": "2026-10-20"}
NOTES = {
    "1-26": "第1-26号の全欄（T42 の追加）。値は services/form_data_application.py が作る。"
    "署名の欄は申請人が手で書く（値を入れない）。",
    "1-11-1": "第1-11-1号の 1〜3 と末尾（T46b、ADR-0038）。値は services/form_data_application.py が"
    "作る。4・5 は支援計画の全部を委託するので記載不要（空のまま）。",
}
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
    "1-26": [
        "[relief:□][relief_y:5]年[relief_m:2]月[relief_d:2]日、納付緩和措置（換価の猶予、納税（納付）の猶予又は"
        "納付受託等）又は社会保険料の納付免除措置の申請しているため。",
        "[other:□]＿上記以外にやむを得ない理由がある場合は、以下に具体的な理由を記載。",
        "＿＿＿＿＿[made_y:5]年[made_m:5]月[made_d:5]日",
    ],
    "1-11-1": [
        "＿＿＿＿＿＿＿＿＿＿[made_y:]年[made_m:5]月[made_d:4]日",
        "＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿特定技能所属機関の氏名又は名称[host_name:]",
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
EMPTY_AFTER_SPEC = {
    # 第1-26号: 「上記以外に…記載。」の次の空の段落に理由の文
    "1-26": [("上記以外にやむを得ない理由がある場合は", "other_reason")],
}
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
    "court", "host_signature", "organization_signature", "full_support", "other_reason",
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
    "relief": "納付緩和措置・納付免除措置を申請している",
    "relief_y": "申請した日（年）",
    "relief_m": "申請した日（月）",
    "relief_d": "申請した日（日）",
    "other": "上記以外のやむを得ない理由",
    "other_reason": "具体的な理由",
}


def label_of(name: str) -> str:
    if name in LABELS:
        return LABELS[name]
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


OVERVIEW_MARKS = "①②③④⑤"
OVERVIEW_MONEY = (("売上高", "sales"), ("経常損益", "ordinary"), ("純損益", "net"), ("純資産", "assets"))
OVERVIEW_YEARS = ("前年度", "前々年度", "前々々年度")


def _slot(document: docx_tags.Document, index: int, name: str, label: str) -> dict:
    document.append_to_empty(index, docx_tags.default_tag(name, ""))
    LABELS[name] = label
    TEXT.add(name)
    return {"name": name, "original": ""}


def _before_unit(document: docx_tags.Document, index: int, unit: str, name: str, label: str) -> dict:
    """「円」「名」の段落の前に値を置く（値が無ければ単位だけの元の段落）。"""
    if document.text(index) != unit:
        raise docx_tags.TagError(f"paragraph {index} is not {unit!r}: {document.text(index)!r}")
    document.put_range(index, 0, 0, docx_tags.default_tag(name, ""))
    LABELS[name] = label
    return {"name": name, "original": ""}


def _check(document: docx_tags.Document, index: int, name: str, label: str) -> dict:
    document.put_tag(index, "□", name)
    LABELS[name] = label
    return {"name": name, "original": "□"}


def overview_1_11_1(document: docx_tags.Document) -> list[dict]:
    """第1-11-1号の表（1 所属役員・2 決算状況・3 基準適合性）と作成責任者。"""
    slots: list[dict] = []
    start = document.find("所属役員")
    # 1 所属役員: 番号の段落の次が「ふりがな」、その 2 つ後が役職、次の行の 3 つ目の欄が氏名
    for number, mark in enumerate(OVERVIEW_MARKS, start=1):
        index = next(
            i for i in range(start, len(document.paragraphs())) if document.text(i) == mark
        )
        if document.text(index + 2) != "役職":
            raise docx_tags.TagError(f"officer row {number}")
        slots.append(_slot(document, index + 1, f"officer{number}_kana", f"役員{number}（ふりがな）"))
        slots.append(_slot(document, index + 3, f"officer{number}_position", f"役員{number}（役職）"))
        slots.append(_slot(document, index + 6, f"officer{number}_name", f"役員{number}（氏名）"))
        start = index + 1
    # 2 決算状況: 行の見出しの次の 3 つの「円」が前年度・前々年度・前々々年度
    start = document.find("決算状況")
    for heading, key in OVERVIEW_MONEY:
        index = document.find(heading, start=start)
        for year, year_label in enumerate(OVERVIEW_YEARS, start=1):
            name = f"{key}_{year}"
            slots.append(_before_unit(document, index + year, "円", name, f"{heading}（{year_label}）"))
            YEN.add(name)
        start = index + 1
    # 3 (1) 離職状況
    for heading, key in (("日本人労働者", "japanese"), ("外国人労働者", "foreign")):
        index = document.find(heading, start=start)
        slots.append(
            _before_unit(document, index + 1, "名", f"left_{key}_voluntary", f"{heading}（自発的離職者）")
        )
        slots.append(
            _before_unit(
                document, index + 2, "名", f"left_{key}_involuntary", f"{heading}（非自発的離職者）"
            )
        )
        start = index + 1
    # 3 (2) 行方不明者数（特定技能・技能実習）と、責めに帰すべき事由の該当
    for heading, key, label in (("１号及び２号", "ssw", "特定技能"), ("技能実習", "interns", "技能実習")):
        index = next(
            i for i in range(start, len(document.paragraphs())) if document.text(i) == heading
        )
        slots.append(_before_unit(document, index + 1, "名", f"missing_{key}", f"行方不明者数（{label}）"))
        slots.append(_check(document, index + 3, f"missing_{key}_fault", f"{label}: 責めに帰すべき事由 該当あり"))
        slots.append(_check(document, index + 4, f"missing_{key}_no_fault", f"{label}: 該当なし"))
        start = index + 1
    # 作成責任者（「役職・氏名」の後ろの空白をすべて欄にする）
    index = document.find("作成責任者", start=start)
    text = document.text(index)
    position = text.index("役職・氏名") + len("役職・氏名")
    original = text[position:]
    document.put_range(index, position, len(text), docx_tags.default_tag("author", original))
    slots.append({"name": "author", "original": original})
    return slots


def make(form_id: str, document: docx_tags.Document) -> list[dict]:
    slots: list[dict] = []
    start = 0
    for heading, name in EMPTY_AFTER.get(form_id, []):
        index = document.find(heading, start=start)
        document.append_to_empty(index + 1, docx_tags.default_tag(name, ""))
        slots.append({"name": name, "original": ""})
        start = index + 1
    slots += apply(document, SPECS[form_id])
    for heading, name in EMPTY_AFTER_SPEC.get(form_id, []):
        index = document.find(heading)
        document.append_to_empty(index + 1, docx_tags.default_tag(name, ""))
        slots.append({"name": name, "original": ""})
    if form_id == "5-10":
        slots += fee_rows(document)
    if form_id == "1-25":
        slots += author_1_25(document)
    if form_id == "1-11-1":
        slots = overview_1_11_1(document) + slots
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
        "valid_from": VALID_FROMS.get(form_id, VALID_FROM),
        "note": NOTES.get(
            form_id, "T37 の全欄（ADR-0026）。値は services/form_data_application.py が作る。"
        ),
        "fields": items,
        "rules": [],
    }
    print(json.dumps(mapping, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in SPECS:
        raise SystemExit(
            "usage: make_application_templates.py <3-1|13-1|13-2|5-10|1-25|1-26|1-11-1> <folder>"
        )
    main(sys.argv[1], sys.argv[2])
