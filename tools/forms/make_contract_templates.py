"""雇用契約の書類（第1-4号・第1-5号・第1-6号）の template.docx とマッピング定義の JSON を作る（開発用。
T34、ADR-0023）。

original（第1-4号は .doc を変換した converted.docx。tools/forms/convert_doc.py）の word/document.xml に
docxtpl の差込タグを置くだけで、ほかの zip の中身はそのまま写す。出力は手で直さない（直すときは
このスクリプトを直して作り直す。テストで、出力が登録済みのファイルと同じことを確かめる）。
標準ライブラリだけで動く。

欄の書き方（下の SPEC）: 段落の文字を、「＿」を全角空白として書き、差し込む欄を [名前:元の文字] で囲む。
元の文字が数字だけなら、その数の全角空白。段落の文字と一致しなければ止まる（様式が変わった）。
先頭が「…」なら段落の終わりの部分だけ、終わりが「…」なら段落の初めの部分だけを書いている。

- タグは {{ 名前 or "元の文字" }}。値が無いときは元の文字（空白や □）に戻る。値を何も入れずに
  差し込むと、本文は original と同じ（別紙２ を除く）。
- 空白の欄は、値を欄の幅にそろえる（pad。数字は右、文字は左に寄せる。T34-D6・ADR-0021 と同じ）。
- 第1-6号の別紙２（無期転換後の条件）と、その後ろの最後の空の段落は {%p if annex2 %} 〜
  {%p endif %} で囲み、生成では出さない（T34-D3。元データが無い。別紙２ は契約の版の添付で扱う）。

使い方（リポジトリの直下で）:
    python3 tools/forms/make_contract_templates.py 1-6 templates/forms/1-6/2026-10-18 \\
        > mappings/1-6/2026-10-18.json
    （1-5・1-4 も同じ。template.docx を版のフォルダに書き、マッピング定義を標準出力に出す）
"""

from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import docx_tags  # noqa: E402

SP = "　"
SLOT = re.compile(r"\[([a-z0-9_]+):([^\]]*)\]")
VALID_FROM = "2026-10-18"


def _expand(text: str) -> str:
    return text.replace("＿", SP)


def parse(markup: str) -> tuple[str, list[tuple[str, int, str]]]:
    """(元の段落の文字, [(名前, 位置, 元の文字)])。"""
    plain = ""
    slots: list[tuple[str, int, str]] = []
    cursor = 0
    for match in SLOT.finditer(markup):
        plain += _expand(markup[cursor : match.start()])
        original = match.group(2)
        original = SP * int(original) if original.isdigit() else _expand(original)
        slots.append((match.group(1), len(plain), original))
        plain += original
        cursor = match.end()
    plain += _expand(markup[cursor:])
    return plain, slots


def width(text: str) -> float:
    """全角を 1、半角を 0.5 として数えた幅。"""
    return sum(0.5 if ord(char) < 0x2000 else 1 for char in text)


def apply(
    document: docx_tags.Document, spec: list[str], stop: int | None = None, start: int = 0
) -> list[dict]:
    """SPEC の段落を前から順に探してタグを置き、欄の一覧（名前・元の文字）を返す。"""
    found: list[dict] = []
    for markup in spec:
        suffix = markup.startswith("…")
        prefix = markup.endswith("…") and not suffix
        body = markup[1:] if suffix else markup[:-1] if prefix else markup
        plain, slots = parse(body)
        index = None
        for i in range(start, stop if stop is not None else len(document.paragraphs())):
            text = document.text(i)
            if suffix:
                matched = text.endswith(plain)
            elif prefix:
                matched = text.startswith(plain)
            else:
                matched = text == plain
            if matched:
                index = i
                break
        if index is None:
            raise docx_tags.TagError(f"paragraph not found: {plain!r}")
        base = len(document.text(index)) - len(plain) if suffix else 0
        for name, position, original in reversed(slots):
            begin = base + position
            document.put_range(index, begin, begin + len(original), docx_tags.default_tag(name, original))
        found.extend({"name": name, "original": original} for name, _p, original in slots)
        start = index + 1
    return found


# --- 第1-6号 雇用条件書 ---------------------------------------------------------------

ARTICLES_3 = "第＿＿条～第＿＿条、第＿＿条～第＿＿条、第＿＿条～第＿＿条"
ARTICLES_2 = "第＿＿条～第＿＿条、第＿＿条～第＿＿条"
SHIFT = (
    "＿＿＿始業（[shift{n}_start_h:2]時[shift{n}_start_mi:2]分）＿終業（[shift{n}_end_h:2]時"
    "[shift{n}_end_mi:2]分）＿（適用日[shift{n}_applies:6]、１日の所定労働時間[shift{n}_daily_h:3]"
    "時間[shift{n}_daily_mi:2]分）"
)
ALLOWANCE = (
    "＿＿({letter}) （[allowance_{letter}_name:5]手当[allowance_{letter}_amount:5]円／計算方法："
    "[allowance_{letter}_method:10]）"
)
OTHER_DEDUCTION = "＿＿＿＿＿＿ ＿（[deduction_other{n}_name:5]） （約[deduction_other{n}_amount:6]円）"

SPEC_1_6 = [
    "[signed_y:]年[signed_m:3]月[signed_d:3]日＿",
    "[worker_name:15]殿＿",
    "特定技能所属機関名＿[host_name:17]",
    "所在地＿＿＿＿＿＿＿[host_address:17]",
    "電話番号＿＿＿＿＿＿[host_phone:17]",
    "代表者＿役職・氏名＿[host_representative:16]",
    "＿＿（[starts_y:4]年[starts_m:2]月[starts_d:2]日＿～＿[ends_y:4]年[ends_m:2]月[ends_d:2]日）"
    "＿＿＿＿＿＿＿＿入国予定日[entry_y:4]年[entry_m:2]月[entry_d:2]日",
    "[renewal_automatic:□]＿自動的に更新する＿＿＿＿＿[renewal_possible:□]＿更新する場合があり得る"
    "＿＿＿[renewal_none:□]＿契約の更新はしない",
    "[criterion_workload:□]＿契約期間満了時の業務量＿＿＿＿[criterion_performance:□]＿労働者の勤務成績、"
    "態度＿＿＿＿＿[criterion_ability:□]＿労働者の業務を遂行する能力",
    "[criterion_business:□]＿会社の経営状況＿＿＿＿＿＿＿＿[criterion_progress:□]＿従事している業務の"
    "進捗状況＿＿＿[criterion_other:□]＿その他（[criterion_other_text:19]）",
    "３． 更新上限の有無（[renewal_limit:無・有（更新＿＿回まで／通算契約期間＿＿年まで）]）",
    "…末日の翌日（ [conversion_y:2]年 [conversion_m:2]月 [conversion_d:2]日）から、無期雇用契約での雇用に"
    "転換することができる。この場合の本契約からの労働条件の変更の有無（[conversion_change: 無 ・ 有"
    "（別紙２のとおり）]）",
    "[direct_employment:□]＿直接雇用（以下に記入）",
    "事業所名＿[site_name:19]",
    "所在地＿[site_address:19]連絡先＿[site_phone:19]",
    "（変更の範囲）[workplace_unchanged:□]変更の可能性なし（変更ある場合は以下に記入）",
    "事業所名＿[workplace_change:19]",
    "１．分＿＿野（[field:11]）＿＿＿＿２．業務区分（[categories:11]）＿＿＿ ",
    "（変更の範囲）[duties_unchanged:□]変更の可能性なし（変更ある場合は以下に記入）",
    "１．分＿＿野（[duty_change:11]）",
    "＿(1) 始業＿＿（[start_h:2]時[start_mi:2]分）＿＿＿終業＿＿（[end_h:2]時[end_mi:2]分）＿＿＿"
    "（１日の所定労働時間数[daily_h:3]時間[daily_mi:2]分）",
    "＿＿[variable_hours:□]＿変形労働時間制：（[variable_hours_unit:4]）単位の変形労働時間制",
    "＿＿[shifts:□]＿交代制として、次の勤務時間の組合せによる。",
    SHIFT.format(n=1),
    SHIFT.format(n=2),
    SHIFT.format(n=3),
    "＿２．休憩時間＿＿（[break_minutes:4]分）",
    "＿３．所定労働時間数＿①週（[weekly_h:3]時間[weekly_mi:4]分）＿②月（[monthly_h:3]時間"
    "[monthly_mi:3]分）＿③年（[annual_h:3]時間[annual_mi:3]分）",
    "＿４．所定労働日数＿＿①週（[weekly_days:4]日）＿＿＿＿＿＿②月（[monthly_days:4]日）＿＿＿＿＿"
    "③年（[annual_days:4]日）",
    "＿５．所定時間外労働の有無＿＿＿[overtime_yes:□]＿有＿＿＿＿[overtime_no:□]＿無",
    f"＿＿＿○詳細は、就業規則＿[articles_hours:{ARTICLES_3}]",
    "＿１．定例日：毎週[holiday_weekdays:3]曜日、[national_holidays:日本の国民の祝日]、その他"
    "（[holidays_other:11]）＿＿（年間合計休日日数[annual_holidays:4]日）",
    "＿２．非定例日：[irregular_holidays:週・月当たり＿＿＿日、その他（＿＿＿＿＿＿＿＿＿＿＿）]",
    f"○詳細は、就業規則＿[articles_holidays:{ARTICLES_2}]",
    "＿１．年次有給休暇＿＿＿６か月継続勤務した場合→[paid_leave_days:4]日",
    "＿＿＿＿＿＿＿＿＿＿＿＿継続勤務６か月未満の年次有給休暇（[early_leave_yes:□]＿有＿＿＿＿"
    "[early_leave_no:□]＿無）＿→[early_leave_months:3]か月経過で[early_leave_days:3]日",
    "＿２．その他の休暇＿＿＿有給（[other_paid_leave:11]）＿＿無給（[other_unpaid_leave:11]）",
    f"＿○詳細は、就業規則＿[articles_leave:{ARTICLES_2}]",
    "１．基本賃金＿＿＿[wage_monthly:□]＿月給（[wage_monthly_amount:7]円）＿＿[wage_daily:□]＿日給"
    "（[wage_daily_amount:7]円）＿＿[wage_hourly:□]＿時間給（[wage_hourly_amount:7]円）",
    "＿＿＿＿＿（[allowance1:7]手当、[allowance2:7]手当、[allowance3:7]手当）",
    "＿＿(1) 所定時間外＿＿法定超月60時間以内＿＿（[rate_overtime_within_60h:5]）％",
    "＿＿＿＿＿＿＿＿＿＿＿法定超月60時間超＿＿＿（[rate_overtime_over_60h:5]）％",
    "＿＿＿＿＿＿＿＿＿＿＿所定超＿＿＿＿＿＿＿＿＿（[rate_overtime_scheduled:5]）％",
    "＿＿(2) 休日＿＿＿＿＿法定休日＿＿＿（[rate_holiday_statutory:5]）％、＿＿法定外休日＿＿＿"
    "（[rate_holiday_non_statutory:5]）％",
    "＿＿(3) 深夜＿＿＿＿＿（[rate_night:5]）％",
    "＿４．賃金締切日＿＿＿[wage_closing_day:□＿毎月＿＿＿日、＿□＿毎月＿＿＿日]",
    "＿５．賃金支払日＿＿＿[wage_payment_day:□＿毎月＿＿＿日、＿□＿毎月＿＿＿日]",
    "＿６．賃金支払方法＿＿[payment_bank:□]＿口座振込＿＿＿＿[payment_cash:□]＿通貨払",
    "＿７．労使協定に基づく賃金支払時の控除＿＿[deduction_agreement_no:□]＿無＿＿＿＿"
    "[deduction_agreement_yes:□]＿有",
    "＿８．昇給＿＿＿＿＿＿[raise_yes:□]＿有（時期、金額等[raise_note:24]）、＿[raise_no:□]＿無",
    "＿９．賞与＿＿＿＿＿＿[bonus_yes:□]＿有（時期、金額等[bonus_note:24]）、＿[bonus_no:□]＿無",
    "＿10．退職金＿＿＿＿＿[retirement_yes:□]＿有（時期、金額等[retirement_note:24]）、＿"
    "[retirement_no:□]＿無",
    "＿11. 休業手当＿＿＿＿[leave_allowance:□]＿有（率[leave_allowance_rate:29]）",
    "＿１．自己都合退職の手続（退職する[resignation_notice_days:4]日前に社長・工場長等に届けること）",
    f"○詳細は、就業規則＿[articles_resignation:{ARTICLES_2}]",
    "＿１．社会保険の加入状況・労働保険の適用状況（[insurance_employees_pension:□]＿厚生年金＿、"
    "[insurance_health:□]＿健康保険＿、[insurance_employment:□]＿雇用保険＿、"
    "[insurance_workers_compensation:□]＿労災保険＿、",
    "[insurance_national_pension:□]＿国民年金＿、[insurance_national_health:□]＿国民健康保険＿、"
    "[insurance_other:□]＿その他（[insurance_other_text:6]））",
    "２．雇入れ時の健康診断＿＿＿＿[hiring_check_y:4]年[hiring_check_m:5]月",
    "＿３．初回の定期健康診断＿＿＿＿[first_check_y:4]年[first_check_m:5]月＿（その後"
    "[check_interval:5]ごとに実施）",
    "＿＿＿部署名[treatment_department:7]担当者職氏名[treatment_person:8]（連絡先[treatment_phone:9]）",
    "部署名[consultation_department:7]担当者職氏名[consultation_person:8]（連絡先"
    "[consultation_phone:9]）",
    "以上のほかは、当社就業規則による。就業規則を確認できる場所や方法（[work_rules_location:25]）",
    # 別紙１ 賃金の支払
    "＿＿[annex_wage_monthly:□]＿月給（[annex_wage_monthly_amount:5]円）＿[annex_wage_daily:□]＿日給"
    "（[annex_wage_daily_amount:5]円）＿[annex_wage_hourly:□]＿時間給（[annex_wage_hourly_amount:5]円）",
    "※月給・日給の場合の１時間当たりの金額（[hourly_equivalent:6]円）",
    "※日給・時間給の場合の１か月当たりの金額（[monthly_equivalent:6]円）",
    ALLOWANCE.format(letter="a"),
    ALLOWANCE.format(letter="b"),
    ALLOWANCE.format(letter="c"),
    ALLOWANCE.format(letter="d"),
    "(e) （[fixed_overtime_name:5]手当[fixed_overtime_amount:5]円",
    "＿・支給要件：時間外労働の有無にかかわらず、[fixed_overtime_hours:5]時間分の時間外手当として支給。",
    "＿[fixed_overtime_hours_over:5]時間を超える時間外労働分についての割増賃金は追加で支給。）",
    "３．１か月当たりの支払概算額（１＋２）＿＿＿＿＿＿ ＿＿＿約[monthly_total:9]円（合計）",
    "＿＿(a) 税＿＿＿金＿＿＿＿＿  （約[deduction_tax:6]円）",
    "＿＿(b) 社会保険料＿＿＿＿＿  （約[deduction_social_insurance:6]円）",
    "＿＿(c) 雇用保険料＿＿＿＿＿  （約[deduction_employment_insurance:6]円）",
    "＿＿(d) 食＿＿＿費＿＿＿＿ ＿ （約[deduction_food:6]円）",
    "＿＿(e) 居＿住＿費＿＿＿＿＿  （約[deduction_housing:6]円）",
    "＿＿(f) その他 （水道光熱費） （約[deduction_utilities:6]円）",
    *(OTHER_DEDUCTION.format(n=n) for n in range(1, 6)),
    "控除する金額＿＿約[deduction_total:9]円（合計）",
    "５．手取り支給額（３－４）＿＿＿＿＿＿＿＿＿＿＿ 約[take_home:9]円（合計）",
]

# 同じ値を 2 か所に書く欄（別紙１ の基本賃金と、本体の基本賃金・固定残業の時間）
SAME_KEY = {
    "annex_wage_monthly": "wage_monthly",
    "annex_wage_daily": "wage_daily",
    "annex_wage_hourly": "wage_hourly",
    "annex_wage_monthly_amount": "wage_monthly_amount",
    "annex_wage_daily_amount": "wage_daily_amount",
    "annex_wage_hourly_amount": "wage_hourly_amount",
    "fixed_overtime_hours_over": "fixed_overtime_hours",
}

# 文字の欄（左に寄せる。数字は全角にする）。ここに無い空白の欄は数字（右に寄せる）
TEXT_FIELDS_1_6 = {
    "worker_name": "text",
    "host_name": "text",
    "host_address": "zen_text",
    "host_phone": "zen_text",
    "host_representative": "text",
    "criterion_other_text": "zen_text",
    "renewal_limit": "text",
    "conversion_change": "text",
    "site_name": "text",
    "site_address": "zen_text",
    "site_phone": "zen_text",
    "workplace_change": "zen_text",
    "field": "text",
    "categories": "text",
    "duty_change": "zen_text",
    "variable_hours_unit": "text",
    "articles_hours": "zen_text",
    "articles_holidays": "zen_text",
    "articles_leave": "zen_text",
    "articles_resignation": "zen_text",
    "holiday_weekdays": "text",
    "national_holidays": "text",
    "holidays_other": "zen_text",
    "irregular_holidays": "zen_text",
    "other_paid_leave": "zen_text",
    "other_unpaid_leave": "zen_text",
    "wage_closing_day": "zen_text",
    "wage_payment_day": "zen_text",
    "raise_note": "zen_text",
    "bonus_note": "zen_text",
    "retirement_note": "zen_text",
    "leave_allowance_rate": "zen_text",
    "articles_resignation_": "zen_text",
    "insurance_other_text": "zen_text",
    "check_interval": "zen_text",
    "treatment_department": "zen_text",
    "treatment_person": "text",
    "treatment_phone": "zen_text",
    "consultation_department": "zen_text",
    "consultation_person": "text",
    "consultation_phone": "zen_text",
    "work_rules_location": "zen_text",
    "fixed_overtime_name": "zen_text",
    **{f"shift{n}_applies": "zen_text" for n in range(1, 4)},
    **{f"allowance{n}": "zen_text" for n in range(1, 4)},
    **{f"allowance_{x}_name": "zen_text" for x in "abcd"},
    **{f"allowance_{x}_method": "zen_text" for x in "abcd"},
    **{f"deduction_other{n}_name": "zen_text" for n in range(1, 6)},
}
YEN_FIELDS_1_6 = {
    "wage_monthly_amount",
    "wage_daily_amount",
    "wage_hourly_amount",
    "annex_wage_monthly_amount",
    "annex_wage_daily_amount",
    "annex_wage_hourly_amount",
    "hourly_equivalent",
    "monthly_equivalent",
    "fixed_overtime_amount",
    "monthly_total",
    "deduction_total",
    "take_home",
    *{f"allowance_{x}_amount" for x in "abcd"},
    *{f"deduction_{kind}" for kind in (
        "tax", "social_insurance", "employment_insurance", "food", "housing", "utilities"
    )},
    *{f"deduction_other{n}_amount" for n in range(1, 6)},
}
MINUTE_FIELDS = re.compile(r".*_mi$")  # 分は 2 けた（「００」。記載例と同じ）

LABELS_1_6 = {
    "signed": "冒頭の日付（締結日）",
    "worker_name": "特定技能外国人の氏名（殿）",
    "host_name": "特定技能所属機関名",
    "host_address": "所在地",
    "host_phone": "電話番号",
    "host_representative": "代表者 役職・氏名",
    "starts": "Ⅰ１ 雇用契約期間（始期）",
    "ends": "Ⅰ１ 雇用契約期間（終期）",
    "entry": "Ⅰ１ 入国予定日",
    "renewal": "Ⅰ２ 契約の更新の有無",
    "criterion": "Ⅰ２ 更新の判断基準",
    "renewal_limit": "Ⅰ３ 更新上限の有無",
    "conversion": "Ⅰ 無期雇用契約への転換",
    "direct_employment": "Ⅱ 直接雇用",
    "site": "Ⅱ 就業の場所（事業所）",
    "workplace": "Ⅱ 就業の場所（変更の範囲）",
    "field": "Ⅲ１ 分野",
    "categories": "Ⅲ２ 業務区分",
    "duties_unchanged": "Ⅲ 業務（変更の範囲）",
    "duty_change": "Ⅲ 業務（変更の範囲）",
    "start": "Ⅳ１(1) 始業",
    "end": "Ⅳ１(1) 終業",
    "daily": "Ⅳ１(1) １日の所定労働時間数",
    "variable_hours": "Ⅳ１(2) 変形労働時間制",
    "shifts": "Ⅳ１(2) 交代制",
    "shift": "Ⅳ１(2) 交代制の勤務時間",
    "break_minutes": "Ⅳ２ 休憩時間",
    "weekly": "Ⅳ３ 所定労働時間数（週）",
    "monthly": "Ⅳ３ 所定労働時間数（月）",
    "annual": "Ⅳ３ 所定労働時間数（年）",
    "weekly_days": "Ⅳ４ 所定労働日数（週）",
    "monthly_days": "Ⅳ４ 所定労働日数（月）",
    "annual_days": "Ⅳ４ 所定労働日数（年）",
    "overtime": "Ⅳ５ 所定時間外労働の有無",
    "articles_hours": "Ⅳ 就業規則の条",
    "holiday_weekdays": "Ⅴ１ 定例日（曜日）",
    "national_holidays": "Ⅴ１ 定例日（日本の国民の祝日）",
    "holidays_other": "Ⅴ１ 定例日（その他）",
    "annual_holidays": "Ⅴ１ 年間合計休日日数",
    "irregular_holidays": "Ⅴ２ 非定例日",
    "articles_holidays": "Ⅴ 就業規則の条",
    "paid_leave_days": "Ⅵ１ 年次有給休暇",
    "early_leave": "Ⅵ１ 継続勤務６か月未満の年次有給休暇",
    "other_paid_leave": "Ⅵ２ その他の休暇（有給）",
    "other_unpaid_leave": "Ⅵ２ その他の休暇（無給）",
    "articles_leave": "Ⅵ 就業規則の条",
    "wage": "Ⅶ１ 基本賃金",
    "allowance1": "Ⅶ２ 諸手当（１つ目）",
    "allowance2": "Ⅶ２ 諸手当（２つ目）",
    "allowance3": "Ⅶ２ 諸手当（３つ目）",
    "rate": "Ⅶ３ 割増賃金率",
    "wage_closing_day": "Ⅶ４ 賃金締切日",
    "wage_payment_day": "Ⅶ５ 賃金支払日",
    "payment": "Ⅶ６ 賃金支払方法",
    "deduction_agreement": "Ⅶ７ 労使協定に基づく賃金支払時の控除",
    "raise": "Ⅶ８ 昇給",
    "bonus": "Ⅶ９ 賞与",
    "retirement": "Ⅶ10 退職金",
    "leave_allowance": "Ⅶ11 休業手当",
    "resignation_notice_days": "Ⅷ１ 自己都合退職の手続",
    "articles_resignation": "Ⅷ 就業規則の条",
    "insurance": "Ⅸ１ 社会保険・労働保険",
    "hiring_check": "Ⅸ２ 雇入れ時の健康診断",
    "first_check": "Ⅸ３ 初回の定期健康診断",
    "check_interval": "Ⅸ３ 定期健康診断の間隔",
    "treatment": "Ⅸ４ 待遇の相違の説明の窓口",
    "consultation": "Ⅸ５ 雇用管理の改善等の相談窓口",
    "work_rules_location": "就業規則を確認できる場所や方法",
    "annex_wage": "別紙１ １ 基本賃金",
    "hourly_equivalent": "別紙１ １ １時間当たりの金額",
    "monthly_equivalent": "別紙１ １ １か月当たりの金額",
    "allowance_a": "別紙１ ２(a) 諸手当",
    "allowance_b": "別紙１ ２(b) 諸手当",
    "allowance_c": "別紙１ ２(c) 諸手当",
    "allowance_d": "別紙１ ２(d) 諸手当",
    "fixed_overtime": "別紙１ ２(e) 固定残業代",
    "monthly_total": "別紙１ ３ １か月当たりの支払概算額",
    "deduction_tax": "別紙１ ４(a) 税金",
    "deduction_social_insurance": "別紙１ ４(b) 社会保険料",
    "deduction_employment_insurance": "別紙１ ４(c) 雇用保険料",
    "deduction_food": "別紙１ ４(d) 食費",
    "deduction_housing": "別紙１ ４(e) 居住費",
    "deduction_utilities": "別紙１ ４(f) 水道光熱費",
    "deduction_other": "別紙１ ４ その他の控除",
    "deduction_total": "別紙１ ４ 控除する金額",
    "take_home": "別紙１ ５ 手取り支給額",
}

# --- 第1-5号 特定技能雇用契約書 ----------------------------------------------------------

SPEC_1_5 = [
    "特定技能所属機関[host_name:＿＿＿＿＿＿＿＿＿ ＿＿＿＿＿＿＿＿＿＿＿＿＿]（以下「甲」という。）と",
    "特定技能外国人（候補者を含む。）[worker_name:15]（以下「乙」という。）は、",
    "＿＿＿＿＿＿[signed_y:4]年[signed_m:4]月[signed_d:4]日＿＿締結",
    "甲＿[host_signature:18]＿㊞＿＿＿＿＿＿＿乙＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿",
]
TEXT_FIELDS_1_5 = {"host_name": "text", "worker_name": "text", "host_signature": "text"}
LABELS_1_5 = {
    "host_name": "特定技能所属機関（甲）",
    "worker_name": "特定技能外国人（乙）",
    "signed": "締結日",
    "host_signature": "甲（特定技能所属機関名・代表者役職名・氏名）",
}

# --- 第1-4号 特定技能外国人の報酬に関する説明書 -------------------------------------------------

AGE_SEX = "（[{p}_age:5]歳）＿＿（[{p}_sex:＿男＿・＿女＿]）＿（経験[{p}_experience:4]年）"
WAGE = " 月給[{p}_monthly:9]円＿／＿時間給[{p}_hourly:8]円"
RULES_WAGE = "月給[{p}_rules_monthly:9]円＿／＿時間給[{p}_rules_hourly:8]円"
# 中身の無い段落（表の欄）には、段落の後ろにタグを足す（見出しの段落の次の段落）
EMPTY_AFTER_1_4 = [
    ("①申請人の氏名", "applicant_name"),
    ("②申請人の役職、職務内容、責任の程度", "applicant_duties"),
    ("⑤その他", "applicant_other"),
    ("①比較対象となる日本人労働者の役職、職務内容、責任の程度", "comparable_duties"),
    ("⑤申請人に対する報酬が日本人が従事する場合の報酬の額と同等以上であると考える理由", "comparable_reason"),
    ("⑥その他", "comparable_other"),
    ("①最も近い職務を担う日本人労働者の職務内容や責任の程度", "closest_duties"),
    ("⑤申請人に対する報酬が日本人が従事する場合の報酬の額と同等以上であると考える理由", "closest_reason"),
    ("⑥その他", "closest_other"),
]
SPEC_1_4 = [
    AGE_SEX.format(p="applicant"),
    WAGE.format(p="applicant"),
    AGE_SEX.format(p="comparable"),
    WAGE.format(p="comparable"),
    "[comparable_has_rules:有＿＿・＿＿無]",
    RULES_WAGE.format(p="comparable"),
    AGE_SEX.format(p="closest"),
    WAGE.format(p="closest"),
    "[closest_has_rules:有＿＿・＿＿無]",
    RULES_WAGE.format(p="closest"),
    "＿＿＿＿＿＿[made_y:4]年[made_m:4]月[made_d:5]日＿＿作成",
    "＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿特定技能所属機関の氏名又は名称[host_name:]",
    "＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿作成責任者＿役職・氏名[author:13]",
]
TEXT_FIELDS_1_4 = {
    "host_name": "text",
    "author": "text",
    **{f"{p}_sex": "text" for p in ("applicant", "comparable", "closest")},
    **{f"{p}_has_rules": "text" for p in ("comparable", "closest")},
}
YEN_PATTERN_1_4 = re.compile(r".*_(monthly|hourly)$")
LABELS_1_4 = {
    "applicant_name": "１① 申請人の氏名",
    "applicant_duties": "１② 申請人の役職、職務内容、責任の程度",
    "applicant_age": "１③ 申請人の年齢",
    "applicant_sex": "１③ 申請人の性別",
    "applicant_experience": "１③ 申請人の経験年数",
    "applicant": "１④ 申請人に対する報酬",
    "applicant_other": "１⑤ その他",
    "comparable_duties": "２① 比較対象となる日本人労働者の役職、職務内容、責任の程度",
    "comparable_age": "２② 比較対象の年齢",
    "comparable_sex": "２② 比較対象の性別",
    "comparable_experience": "２② 比較対象の経験年数",
    "comparable": "２③ 比較対象となる日本人労働者の報酬",
    "comparable_has_rules": "２④ 賃金規程の有無",
    "comparable_rules": "２④ 賃金規程に基づく報酬",
    "comparable_reason": "２⑤ 同等以上であると考える理由",
    "comparable_other": "２⑥ その他",
    "closest_duties": "３① 最も近い職務を担う日本人労働者の職務内容や責任の程度",
    "closest_age": "３② 最も近い職務を担う日本人労働者の年齢",
    "closest_sex": "３② 最も近い職務を担う日本人労働者の性別",
    "closest_experience": "３② 最も近い職務を担う日本人労働者の経験年数",
    "closest": "３③ 最も近い職務を担う日本人労働者の報酬",
    "closest_has_rules": "３④ 賃金規程の有無",
    "closest_rules": "３④ 賃金規程に基づく報酬",
    "closest_reason": "３⑤ 同等以上であると考える理由",
    "closest_other": "３⑥ その他",
    "made": "作成日",
    "host_name": "特定技能所属機関の氏名又は名称",
    "author": "作成責任者 役職・氏名",
}


def label_of(name: str, labels: dict[str, str]) -> str:
    """欄の名前（長いほうの接頭辞から探す。年月日は「（年）」などを付ける）。"""
    parts = {"_y": "（年）", "_m": "（月）", "_d": "（日）", "_h": "（時）", "_mi": "（分）"}
    for suffix, word in parts.items():
        if name.endswith(suffix) and name[: -len(suffix)] in labels:
            return labels[name[: -len(suffix)]] + word
    candidates = [key for key in labels if name == key or name.startswith(key + "_")]
    if not candidates:
        candidates = [key for key in labels if name.startswith(key)]
    if not candidates:
        raise SystemExit(f"no label for {name}")
    key = max(candidates, key=len)
    if key == name:
        return labels[key]
    rest = name[len(key) :].strip("_")
    return f"{labels[key]}（{rest}）"


def field(prefix: str, slot: dict, *, fmt: str, labels: dict[str, str], align: str) -> dict:
    name, original = slot["name"], slot["original"]
    item: dict = {
        "key": f"{prefix}.{SAME_KEY.get(name, name)}",
        "target": name,
        "format": fmt,
        "label": label_of(name, labels),
    }
    if fmt != "check" and original and not original.strip(SP):
        item["pad"] = width(original)
        item["align"] = align
    return item


def fields_of(
    prefix: str,
    slots: list[dict],
    text_fields: dict[str, str],
    labels: dict[str, str],
    yen: set[str] | re.Pattern[str],
) -> list[dict]:
    items = []
    for slot in slots:
        name, original = slot["name"], slot["original"]
        if original == "□":
            fmt, align = "check", ""
        elif name in text_fields:
            fmt, align = text_fields[name], "left"
        elif (name in yen) if isinstance(yen, set) else yen.match(name):
            fmt, align = "zen_yen", "right"
        elif MINUTE_FIELDS.match(name):
            fmt, align = "zen_2", "right"
        else:
            fmt, align = "zen", "right"
        items.append(field(prefix, slot, fmt=fmt, labels=labels, align=align))
    return items


def make_1_6(document: docx_tags.Document) -> list[dict]:
    annex2 = document.find("参考様式第１－６号" + SP + "別紙２")
    slots = apply(document, SPEC_1_6, stop=annex2)
    # 別紙２ の後ろの最後の空の段落も別紙２ と一緒に外す（残すと、値で 1 行増えた別紙１ の後ろに
    # 空の頁ができる）。本文は別紙１ の最後の段落で終わる
    last = docx_tags.paragraph_spans(document.xml)[-1][1]
    document.insert_paragraph_after_position(last, "{%p endif %}")
    document.insert_paragraph_before(annex2, "{%p if annex2 %}")
    return fields_of("c", slots, TEXT_FIELDS_1_6, LABELS_1_6, YEN_FIELDS_1_6)


def make_1_5(document: docx_tags.Document) -> list[dict]:
    slots = apply(document, SPEC_1_5)
    return fields_of("c", slots, TEXT_FIELDS_1_5, LABELS_1_5, set())


def make_1_4(document: docx_tags.Document) -> list[dict]:
    empty: list[dict] = []
    start = 0
    for heading, name in EMPTY_AFTER_1_4:
        index = document.find(heading, start=start)
        document.append_to_empty(index + 1, docx_tags.default_tag(name, ""))
        empty.append({"name": name, "original": ""})
        start = index + 1
    slots = apply(document, SPEC_1_4)
    return fields_of(
        "w", empty + slots, {**TEXT_FIELDS_1_4, **{s["name"]: "text" for s in empty}},
        LABELS_1_4, YEN_PATTERN_1_4,
    )


FORMS = {
    "1-6": ("original.docx", make_1_6, "第1-6号（雇用条件書）"),
    "1-5": ("original.docx", make_1_5, "第1-5号（特定技能雇用契約書）"),
    "1-4": ("converted.docx", make_1_4, "第1-4号（報酬に関する説明書）"),
}
NOTES = {
    "1-6": "T34 の全欄（ADR-0023）。値は services/form_data_contract.py が作る。別紙２ は生成では出さない（T34-D3）。",
    "1-5": "T34 の全欄（ADR-0023）。乙の署名は手で書く。",
    "1-4": "T34 の全欄（ADR-0023）。比較の型で ２ と ３ のどちらかだけを書く。値は services/form_data_contract.py が作る。",
}


def main(form_id: str, folder: str) -> None:
    source_name, make, _title = FORMS[form_id]
    source = Path(folder) / source_name
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
        "form_id": form_id,
        "valid_from": VALID_FROM,
        "note": NOTES[form_id],
        "fields": items,
        "rules": [],
    }
    print(json.dumps(mapping, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in FORMS:
        raise SystemExit("usage: make_contract_templates.py <1-4|1-5|1-6> <version folder>")
    main(sys.argv[1], sys.argv[2])
