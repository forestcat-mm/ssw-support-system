"""第1-17号（支援計画書）のマッピング定義の JSON を作る（開発用。T31、ADR-0020）。

セルの位置は original.xlsx の結合セルの左上（T31 の計画の調べ。記載例〔全部委託〕と照らした）。
出力は手で直さない（直すときはこのスクリプトを直して作り直す）。テストで、出力が登録済みの JSON と
同じことを確かめる。標準ライブラリだけで動く。

使い方（リポジトリの直下で）:
    python3 tools/forms/make_1_17_mapping.py > mappings/1-17/2026-10-17.json
"""

import json
import sys
from pathlib import Path

SHEET = "参考様式第１－１７号!"
SP = "　"

# 欄の名前（警告の文言に出す。内部の符号は出さない）。区分の題は支援計画の細目のデータから取り、
# services.support_catalog.form_row_name と同じ規則で作る（テストで一致を確かめる）
_CATALOG = (
    Path(__file__).resolve().parents[2] / "backend" / "services" / "data" / "support_plan_1-17.json"
)
SECTION_TITLES = json.loads(_CATALOG.read_text(encoding="utf-8"))["sections"]
FULL_WIDTH_LETTERS = str.maketrans("abcdefghij", "ａｂｃｄｅｆｇｈｉｊ")
METHOD_WORDS = {
    "in_person": "対面", "video_call": "テレビ電話装置", "other": "その他",
    "arrival_airport": "出迎え空港", "departure_airport": "出国予定空港", "transport": "送迎方法",
    "information": "情報提供", "accompany": "同行", "video_or_dvd": "テレビ電話やＤＶＤ等",
    "online": "オンライン",
}
ITEM_SECTIONS = {"pre_guidance": "1", "orientation": "4", "consultation": "6",
                 "periodic_interview": "9"}
DAY_WORDS = {"mon": "月曜日", "tue": "火曜日", "wed": "水曜日", "thu": "木曜日", "fri": "金曜日",
             "sat": "土曜日", "sun": "日曜日", "holiday": "祝日"}


def row_name(code: str) -> str:
    section, _, sub = code.partition(".")
    name = f"Ⅳ{SECTION_TITLES[section]}"
    if sub == "free":
        return f"{name}（自由記入）"
    if sub and sub != "main":
        return f"{name}（{sub.translate(FULL_WIDTH_LETTERS)}）"
    return name


def cell(ref: str) -> str:
    return SHEET + ref


def text(key: str, ref: str, label: str, *, fit: bool = False, fmt: str = "text") -> dict:
    item = {"key": key, "target": cell(ref), "format": fmt, "label": label}
    if fit:
        item["fit"] = True
    return item


def check(key: str, ref: str, label: str) -> dict:
    return {"key": key, "target": cell(ref), "format": "check", "label": label}


def template(ref: str, label: str, pattern: str, keys: dict, *, fit: bool = False) -> dict:
    item = {"target": cell(ref), "format": "template", "template": pattern, "keys": keys}
    item["label"] = label
    if fit:
        item["fit"] = True
    return item


def ymd(key: str, refs: tuple[str, str, str], label: str) -> list[dict]:
    return [
        {"key": key, "target": cell(ref), "format": part, "label": label}
        for ref, part in zip(refs, ("year", "month", "day"))
    ]


def phone(key: str, ref: str, label: str, prefix: str = "電話") -> dict:
    return template(ref, label, f"（{prefix}{SP}{{phone}}）", {"phone": {"key": key}}, fit=True)


# Ⅳ欄の行: 実施予定（有・無の □ と文）・委託・担当者・〒・住所・実施方法
# methods: 実施方法のチェックの位置。notes: 「その他（　）」などの文の位置と型。text: 文で書く実施方法
ROWS = {
    "1.main": dict(yes="188", no="191", outs="AC161", staff="AG161", post="161", addr="AO163",
                   methods={"in_person": "BC161", "video_call": "BC163", "other": "BC165"},
                   notes={"other": ("BD165", "その他（{n}）")}, diff="BC182"),
    "1.free": dict(yes="225", no="228", outs="AC225", staff="AG225", addr="AO225",
                   text="BC225", content="G227"),
    "2.a": dict(yes="244", no="247", outs="AC244", staff="AG244", post="244", addr="AO246",
                methods={"arrival_airport": "BC244", "transport": "BC248"},
                notes={"arrival_airport": ("BE246", "{n}"), "transport": ("BD248", "送迎方法（{n}）")}),
    "2.b": dict(yes="250", no="253", outs="AC250", staff="AG250", post="250", addr="AO252",
                methods={"departure_airport": "BC250", "transport": "BC254"},
                notes={"departure_airport": ("BE252", "{n}"), "transport": ("BD254", "送迎方法（{n}）")}),
    "2.free": dict(yes="256", no="259", outs="AC256", staff="AG256", post="256", addr="AO258",
                   text="BC256", content="E258"),
    "3a.a": dict(yes="276", no="279", outs="AC269", staff="AG269", post="269", addr="AO271",
                 text="BC269"),
    "3a.b": dict(yes="291", no="294", outs="AC290", staff="AG290", post="290", addr="AO292"),
    "3a.c": dict(yes="299", no="302", outs="AC298", staff="AG298", post="298", addr="AO300"),
    "3a.free": dict(yes="306", no="309", outs="AC306", staff="AG306", post="306", addr="AO308",
                    text="BC306", content="G308"),
    "3b.a": dict(yes="338", no="341", outs="AC338", staff="AG338", post="338", addr="AO340",
                 methods={"information": "BC338", "accompany": "BC340", "other": "BC342"},
                 notes={"other": ("BD342", "その他（{n}）")}),
    "3b.b": dict(yes="344", no="347", outs="AC344", staff="AG344", post="344", addr="AO346",
                 methods={"information": "BC344", "accompany": "BC346", "other": "BC348"},
                 notes={"other": ("BD348", "その他（{n}）")}),
    "3b.c": dict(yes="350", no="353", outs="AC350", staff="AG350", post="350", addr="AO352",
                 methods={"information": "BC350", "accompany": "BC352", "other": "BC354"},
                 notes={"other": ("BD354", "その他（{n}）")}),
    "3b.free": dict(yes="356", no="359", outs="AC356", staff="AG356", post="356", addr="AO358",
                    text="BC356", content="G358"),
    "4.main": dict(yes="375", no="379", outs="AC369", staff="AG369", post="369", addr="AO371",
                   methods={"in_person": "BC374", "video_or_dvd": "BC377"}, diff="BC393"),
    "4.free": dict(yes="413", no="416", outs="AC413", staff="AG413", post="413", addr="AO415",
                   text="BC413", content="G415"),
    "5.a": dict(yes="433", no="436", outs="AC432", staff="AG432", post="432", addr="AO434",
                text="BC432"),
    "5.b": dict(yes="443", no="446", outs="AC440", staff="AG440", post="440", addr="AO442"),
    "5.c": dict(yes="453", no="456", outs="AC452", staff="AG452", post="452", addr="AO454"),
    "5.free": dict(yes="460", no="463", outs="AC460", staff="AG460", post="460", addr="AO462",
                   text="BC460", content="E462"),
    "6.main": dict(yes="479", no="482", outs="AC473", staff="AG473", post="473", addr="AO475"),
    "6.free": dict(yes="490", no="493", outs="AC490", staff="AG490", post="490", addr="AO492",
                   content="G492"),
    "7.main": dict(yes="566", no="569", outs="AC554", staff="AG554", post="554", addr="AO556",
                   text="BC554"),
    "7.free": dict(yes="580", no="583", outs="AC580", staff="AG580", post="580", addr="AO582",
                   text="BC580", content="E582"),
    "8.a": dict(yes="593", no="596", outs="AC593", staff="AG593", post="593", addr="AO595",
                text="BC593"),
    "8.b": dict(yes="600", no="603", outs="AC599", staff="AG599", post="599", addr="AO601",
                text="BC599"),
    "8.c": dict(yes="609", no="612", outs="AC607", staff="AG607", post="607", addr="AO609",
                text="BC607"),
    "8.d": dict(yes="618", no="621", outs="AC617", staff="AG617", post="617", addr="AO619",
                text="BC617"),
    "8.e": dict(yes="625", no="628", outs="AC625", staff="AG625", addr="AO625", text="BC625"),
    "8.f": dict(yes="631", no="634", outs="AC631", staff="AG631", post="631", addr="AO633",
                text="BC631"),
    "8.g": dict(yes="639", no="642", outs="AC637", staff="AG637", post="637", addr="AO639",
                text="BC637"),
    "8.free": dict(yes="647", no="650", outs="AC647", staff="AG647", post="647", addr="AO649",
                   text="BC647", content="E649"),
    # 9 は a・b の組（670・673）に書き、c・d の組（687・690）は □ だけ（印刷済みの「有（認知次第
    # 実施）」を残す。T31-D9）。実施方法は「□ 対面」「□ オンライン」の文のセル
    "9.main": dict(yes="670", no="673", outs="AC663", staff="AG663", addr="AO663",
                   checked_methods={"in_person": ("BC664", "対面"), "online": ("BC667", "オンライン")},
                   cd=("687", "690")),
    "9.free": dict(yes="701", no="704", outs="AC701", staff="AG701", post="701", addr="AO703",
                   text="BC701", content="G703"),
}

ITEM_CELLS = {
    # 実施言語（右に「語」が印刷済み）・通訳者（項目名のセルに続けて書く）・実施予定時間
    "pre_guidance": dict(language="S231", interpreter="AC231", hours="V234"),
    "orientation": dict(language="S419", interpreter="AC419", hours="V422"),
    "consultation": dict(language="S544", interpreter="AC544"),
    "periodic_interview": dict(language="S707", interpreter="AC707"),
}
INTERPRETER_LABEL = "（支援担当者以外の者が通訳を担う場合）通訳者の所属・氏名"


# 印刷済みの文（original.xlsx の F711・A764 の文字列そのまま）。差し込む位置だけを {名前} にする
F711_ORIGINAL = (
    "本計画の作成に当たり、地方公共団体が実施する共生社会の実現のための施策（共生施策）を確認しています。\n"
    "共生施策を確認した地方公共団体（市区町村）名\n"
    "\u3000\u3000・支援対象者が活動する事業所の所在地 （\u3000\u3000\u3000\u3000\u3000都・道・府・県"
    "\u3000\u3000\u3000\u3000\u3000市・区・町・村\u3000） \n"
    "\u3000  \u3000\u3000 確認日：\u3000\u3000\u3000\u3000年\u3000\u3000\u3000月\u3000\u3000\u3000日"
    "\u3000\u3000確認方法：\u3000□ ホームページ\u3000 □ その他の方法（" + SP * 17 + "）" + SP * 37 + "\n"
    "\u3000\u3000  \u3000 備考：\n"
    "     ・支援対象者の住居地\n"
    "\u3000\u3000\u3000□ 同上\n"
    "\u3000\u3000\u3000□ 異なる地方公共団体（\u3000\u3000\u3000\u3000\u3000都・道・府・県"
    "\u3000\u3000\u3000\u3000\u3000市・区・町・村\u3000）\u3000\u3000\u3000\n"
    "\u3000\u3000\u3000\u3000\u3000確認日：  \u3000\u3000\u3000年\u3000\u3000\u3000月\u3000\u3000\u3000日"
    "\u3000\u3000確認方法：\u3000□ ホームページ\u3000 □ その他の方法（" + SP * 17 + "）\n"
    "           備考：\n"
    "\u3000"
)
PLACE = "\u3000" * 5 + "都・道・府・県" + "\u3000" * 5 + "市・区・町・村"
METHOD = "□ ホームページ\u3000 □ その他の方法（" + SP * 17 + "）"


def _replace_once(text: str, old: str, new: str) -> str:
    assert old in text, old
    return text.replace(old, new, 1)


def _f711_template() -> str:
    text_ = F711_ORIGINAL
    text_ = _replace_once(text_, "（" + PLACE, "（{sp}{sm}")
    text_ = _replace_once(text_, "（" + PLACE, "（{rp}{rm}")
    text_ = _replace_once(text_, "確認日：\u3000\u3000\u3000\u3000年\u3000\u3000\u3000月\u3000\u3000\u3000日",
                          "確認日：{sy}年{smo}月{sd}日")
    text_ = _replace_once(text_, "確認日：  \u3000\u3000\u3000年\u3000\u3000\u3000月\u3000\u3000\u3000日",
                          "確認日：{ry}年{rmo}月{rd}日")
    text_ = _replace_once(text_, METHOD, "{sw} ホームページ\u3000 {so} その他の方法（{sn}）")
    text_ = _replace_once(text_, METHOD, "{rw} ホームページ\u3000 {ro} その他の方法（{rn}）")
    text_ = _replace_once(text_, "備考：\n     ・", "備考：{snote}\n     ・")
    text_ = _replace_once(text_, "□ 同上", "{same} 同上")
    text_ = _replace_once(text_, "□ 異なる", "{diff} 異なる")
    text_ = _replace_once(text_, "備考：\n\u3000", "備考：{rnote}\n\u3000")
    return text_


F711_TEMPLATE = _f711_template()
_SITE = "municipality.site."
_RESIDENCE = "municipality.residence."
_PREFECTURE = "\u3000" * 5 + "都・道・府・県"
_MUNICIPALITY = "\u3000" * 5 + "市・区・町・村"
F711_KEYS = {
    "sp": {"key": _SITE + "prefecture", "empty": _PREFECTURE, "pattern": "\u3000{}\u3000"},
    "sm": {"key": _SITE + "municipality", "empty": _MUNICIPALITY},
    "sy": {"key": _SITE + "checked_on", "format": "year", "empty": "\u3000" * 4},
    "smo": {"key": _SITE + "checked_on", "format": "month", "empty": "\u3000" * 3},
    "sd": {"key": _SITE + "checked_on", "format": "day", "empty": "\u3000" * 3},
    "sw": {"key": _SITE + "website", "format": "check", "empty": "□"},
    "so": {"key": _SITE + "other", "format": "check", "empty": "□"},
    "sn": {"key": _SITE + "method_note", "empty": SP * 17},
    "snote": {"key": _SITE + "note"},
    "same": {"key": _RESIDENCE + "same", "format": "check", "empty": "□"},
    "diff": {"key": _RESIDENCE + "different", "format": "check", "empty": "□"},
    "rp": {"key": _RESIDENCE + "prefecture", "empty": _PREFECTURE, "pattern": "\u3000{}\u3000"},
    "rm": {"key": _RESIDENCE + "municipality", "empty": _MUNICIPALITY},
    "ry": {"key": _RESIDENCE + "checked_on", "format": "year", "empty": "  \u3000\u3000\u3000"},
    "rmo": {"key": _RESIDENCE + "checked_on", "format": "month", "empty": "\u3000" * 3},
    "rd": {"key": _RESIDENCE + "checked_on", "format": "day", "empty": "\u3000" * 3},
    "rw": {"key": _RESIDENCE + "website", "format": "check", "empty": "□"},
    "ro": {"key": _RESIDENCE + "other", "format": "check", "empty": "□"},
    "rn": {"key": _RESIDENCE + "method_note", "empty": SP * 17},
    "rnote": {"key": _RESIDENCE + "note"},
}
A764_TEMPLATE = (
    " また、Ⅳ欄９アａの定期的な面談を実施する方法について説明を受け、その内容を十分理解し、"
    "\u3000{online} オンライン\u3000 {in_person} 対面\u3000で実施することを希望します。" + SP * 5
)


def row_fields(code: str, spec: dict) -> list[dict]:
    key = f"plan.{code}."
    label = row_name(code)
    fields = [
        check(key + "planned_yes", "S" + spec["yes"], f"{label} 実施予定（有）"),
        check(key + "planned_no", "S" + spec["no"], f"{label} 実施予定（無）"),
        template("T" + spec["yes"], f"{label} 実施予定（有）", "有（{n}）",
                 {"n": {"key": key + "yes_note"}}, fit=True),
        template("T" + spec["no"], f"{label} 実施予定（無）", "無（{n}）",
                 {"n": {"key": key + "no_note"}}, fit=True),
        text(key + "outsourced", spec["outs"], f"{label} 委託の有無"),
        text(key + "staff", spec["staff"], f"{label} 担当者", fit=True),
        text(key + "address", spec["addr"], f"{label} 委託先の住所"),
    ]
    if "cd" in spec:
        fields.append(check(key + "planned_yes", "S" + spec["cd"][0], f"{label} 実施予定（ｃ・ｄ）（有）"))
        fields.append(check(key + "planned_no", "S" + spec["cd"][1], f"{label} 実施予定（ｃ・ｄ）（無）"))
    for method, ref in spec.get("methods", {}).items():
        fields.append(check(key + f"method.{method}", ref, f"{label} 実施方法（{METHOD_WORDS[method]}）"))
    for method, (ref, pattern) in spec.get("notes", {}).items():
        fields.append(template(ref, f"{label} 実施方法（{METHOD_WORDS[method]}）", pattern,
                               {"n": {"key": key + f"method_note.{method}"}}, fit=True))
    for method, (ref, word) in spec.get("checked_methods", {}).items():
        fields.append(template(ref, f"{label} 実施方法（{word}）", "{c} " + word,
                               {"c": {"key": key + f"method.{method}", "format": "check",
                                      "empty": "□"}}))
    if "text" in spec:
        fields.append(text(key + "method_text", spec["text"], f"{label} 実施方法"))
    if "content" in spec:
        fields.append(text(key + "content", spec["content"], f"{label} 内容"))
    if "diff" in spec:
        fields.append(text(key + "note", spec["diff"], f"{label} 支援内容ごとに異なる場合"))
    return fields


def fields() -> list[dict]:
    result: list[dict] = []
    # 冒頭・Ⅰ
    result += ymd("plan.created_on", ("BE13", "BJ13", "BM13"), "作成日")
    result += [
        text("worker.name_romaji", "O18", "Ⅰ 支援対象者 １ 氏名", fit=True),
        text("worker.sex", "BC18", "Ⅰ 支援対象者 ２ 性別"),
    ]
    result += ymd("worker.birth_date", ("Q26", "V26", "Z26"), "Ⅰ 支援対象者 ３ 生年月日")
    result.append(text("worker.nationality", "BC26", "Ⅰ 支援対象者 ４ 国籍・地域", fit=True))
    # Ⅱ（３・４は全部委託では書かない）
    result += [
        text("host.name_kana", "O34", "Ⅱ １ ふりがな", fit=True),
        text("host.name", "O37", "Ⅱ １ 氏名又は名称", fit=True),
        text("host.postal_first", "P42", "Ⅱ ２ 郵便番号"),
        text("host.postal_last", "V42", "Ⅱ ２ 郵便番号"),
        text("host.address", "O44", "Ⅱ ２ 住所"),
        phone("host.phone", "BB42", "Ⅱ ２ 電話"),
    ]
    # Ⅲ
    result += [
        text("organization.registration_first", "O82", "Ⅲ １ 登録番号"),
        text("organization.registration_last", "AB82", "Ⅲ １ 登録番号"),
    ]
    result += ymd("organization.support_start_planned_on", ("BB82", "BH82", "BL82"),
                  "Ⅲ ２ 登録年月日（支援業務を開始する予定年月日）")
    result += [
        text("organization.name_kana", "O90", "Ⅲ ３ ふりがな", fit=True),
        text("organization.name", "O93", "Ⅲ ３ 氏名又は名称", fit=True),
        text("organization.postal_first", "P98", "Ⅲ ４ 郵便番号"),
        text("organization.postal_last", "V98", "Ⅲ ４ 郵便番号"),
        text("organization.address", "O100", "Ⅲ ４ 住所", fit=True),
        phone("organization.phone", "BB103", "Ⅲ ４ 電話"),
        text("organization.representative_name_kana", "O106", "Ⅲ ５ 代表者 ふりがな", fit=True),
        text("organization.representative_name", "O109", "Ⅲ ５ 代表者の氏名", fit=True),
        text("office.postal_first", "P122", "Ⅲ ６ 郵便番号"),
        text("office.postal_last", "V122", "Ⅲ ６ 郵便番号"),
        text("office.address", "O124", "Ⅲ ６ 支援を行う事務所の所在地", fit=True),
        phone("office.phone", "BB127", "Ⅲ ６ 電話"),
        text("manager.name_kana", "AI130", "Ⅲ ７ 支援責任者 ふりがな", fit=True),
        text("manager.name", "AI133", "Ⅲ ７ 支援責任者 氏名", fit=True),
        text("manager.position", "BE130", "Ⅲ ７ 支援責任者 役職", fit=True),
        text("organization.worker_count", "AC138", "Ⅲ ７ 支援を行っている1号特定技能外国人数",
             fmt="number"),
        text("organization.staff_count", "AC146", "Ⅲ ７ 支援担当者数", fmt="number"),
        text("declaration.propriety", "AZ148", "Ⅲ ７ 支援の適正性を確保していることの有無"),
        text("declaration.propriety_no", "BF148", "Ⅲ ７ 支援の適正性を確保していることの有無（無）"),
    ]
    # Ⅳ
    for code, spec in ROWS.items():
        result += row_fields(code, spec)
    for item, spec in ITEM_CELLS.items():
        key = f"plan.item.{item}."
        section = f"Ⅳ{SECTION_TITLES[ITEM_SECTIONS[item]]}"
        result.append(text(key + "languages", spec["language"], f"{section} 実施言語"))
        result.append(template(spec["interpreter"], f"{section} 通訳者",
                               INTERPRETER_LABEL + SP + SP + "{n}",
                               {"n": {"key": key + "interpreter"}}, fit=True))
        if "hours" in spec:
            result.append(text(key + "planned_hours", spec["hours"], f"{section} 実施予定時間",
                               fmt="number"))
    result += [
        check("housing.secured", "S312", "Ⅳ ３ア ｄ 申請時点で確保"),
        check("housing.after", "S315", "Ⅳ ３ア ｄ 申請後に確保"),
        text("housing.occupants", "AD318", "Ⅳ ３ア ｄ 同居人数", fmt="number"),
        check("housing.room_area_ok", "T321", "Ⅳ ３ア ｄ 7.5㎡以上"),
        check("housing.bedroom_area_ok", "T328", "Ⅳ ３ア ｄ 4.5㎡以上"),
        check("housing.rent_yes", "AR318", "Ⅳ ３ア ｄ ① 居住費の徴収 有"),
        check("housing.rent_no", "AU318", "Ⅳ ３ア ｄ ① 居住費の徴収 無"),
        text("housing.monthly_rent", "AZ321", "Ⅳ ３ア ｄ ② 月額", fmt="number"),
        check("housing.owned", "AW324", "Ⅳ ３ア ｄ ③ 自己所有物件"),
        check("housing.leased", "BD324", "Ⅳ ３ア ｄ ③ 借上物件"),
        text("housing.rent_explanation", "AV327", "Ⅳ ３ア ｄ ④ 適正な額である旨の説明", fit=True),
    ]
    hours = {"mon": "S499", "tue": "AC499", "wed": "AM499", "thu": "AW499", "fri": "BG499",
             "sat": "S502", "sun": "S506", "holiday": "S510"}
    for day, ref in hours.items():
        key = f"consultation.{day}."
        result.append(template(ref, f"Ⅳ６イ 対応時間（{DAY_WORDS[day]}）", "{s}" + SP + "～" + SP + "{e}",
                               {"s": {"key": key + "start"}, "e": {"key": key + "end"}}, fit=True))
    for prefix, row in (("consultation", 517), ("emergency", 532)):
        label = "Ⅳ ６イ 相談方法" if prefix == "consultation" else "Ⅳ ６イ 緊急時"
        result.append(check(f"{prefix}.method.in_person", f"I{row}", f"{label} 直接面談"))
        for offset, method, word in ((3, "phone", "電話"), (6, "email", "メール"), (9, "other", "その他")):
            result.append(check(f"{prefix}.method.{method}", f"I{row + offset}", f"{label} {word}"))
            result.append(template(f"J{row + offset}", f"{label} {word}", word + "（{n}）",
                                   {"n": {"key": f"{prefix}.{method}"}}, fit=True))
    # Ⅴ（「都・道・府・県」「市・区・町・村」を丸で囲む代わりに実名を書く。T31-D3）
    result.append(check("municipality.checked", "E711", "Ⅴ 共生施策の確認"))
    result.append(template("F711", "Ⅴ 共生施策を確認した地方公共団体", F711_TEMPLATE, F711_KEYS))
    # 署名の欄（署名日・本人の署名は手で書く）
    result += [
        text("host.name", "AP748", "特定技能所属機関の氏名又は名称", fit=True),
        text("plan.responsible_name", "AP753", "作成責任者の氏名", fit=True),
        text("plan.translation_language", "H760", "翻訳文の言語"),
        template("A764", "面談の方法の希望", A764_TEMPLATE, {
            "online": {"key": "plan.interview_online", "format": "check", "empty": "□"},
            "in_person": {"key": "plan.interview_in_person", "format": "check", "empty": "□"},
        }),
    ]
    return result


def mapping() -> dict:
    return {
        "form_id": "1-17",
        "valid_from": "2026-10-17",
        "note": (
            "T31 の全欄（ADR-0020）。tools/forms/make_1_17_mapping.py の出力で、手で直さない。"
            "Ⅱ欄３・４は全部委託では書かない（注意２）。署名日・本人の署名は手で書く。"
        ),
        "fit_min_size": 6,
        "fields": fields(),
        "rules": [],
    }


if __name__ == "__main__":
    json.dump(mapping(), sys.stdout, ensure_ascii=False, indent=1)
    sys.stdout.write("\n")
