"""日中併記の様式（第1-5号・第1-6号・第5-8号・第5-9号）の template.docx とマッピング定義を作る（開発用。
T38、ADR-0027）。

元は extract_zh_forms.py が入管の中国語の版から取り出した extracted.docx。差込タグを置くだけで、
ほかの zip の中身はそのまま写す。値は日本語の行の欄と、中国語の行の同じ欄の両方に入れる（修正 1 回目
A7。T38-D6 では日本語の行だけだった）。中国語の行のタグは日本語の行と同じ名前なので、マッピングの欄は
増えない。
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
from make_contract_templates import (  # noqa: E402
    LABELS_1_5,
    LABELS_1_6,
    SPEC_1_6,
    TEXT_FIELDS_1_5,
    TEXT_FIELDS_1_6,
    YEN_FIELDS_1_6,
    apply,
    fields_of,
)

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


# 中国語の行（修正 1 回目 A7）: 日本語の行と同じ名前のタグを置き、同じ値を入れる
ZH_UNITS_5_8 = ("年", "月", "日", "点", "分至", "点", "分")
ZH_UNITS_5_9 = ("年", "月", "日", "点", "分起至", "点", "分为止")
HALF = " "  # 半角の空白（併記の版の中国語の行は、欄の一部が半角の空白で作られている）


def zh_row(row: int, indent: str, units: tuple[str, ...], year: str = "＿＿＿＿", slot: str = "＿＿") -> str:
    names = ("year", "month", "day", "start_hour", "start_minute", "end_hour", "end_minute")
    widths = (year,) + (slot,) * 6
    return indent + "".join(
        f"[s{row}_{name}:{width}]{unit}" for name, width, unit in zip(names, widths, units, strict=True)
    )


SPEC_ZH = {
    "5-8": [
        zh_row(1, "", ZH_UNITS_5_8),
        zh_row(2, "＿" * 5, ZH_UNITS_5_8),
        zh_row(3, "＿" * 5, ZH_UNITS_5_8),
    ],
    "5-9": [
        zh_row(1, "＿" * 5, ZH_UNITS_5_9, slot=HALF * 4),
        zh_row(2, "＿" * 6, ZH_UNITS_5_9, year="＿＿" + HALF * 2, slot=HALF * 4),
        zh_row(3, "＿" * 5, ZH_UNITS_5_9, slot=HALF * 4),
    ],
    "1-5": [
        "特定技能所属机构[host_name:" + "＿" * 15 + HALF * 16 + "]（以下简称“甲方”）与",
        "特定技能外国人（含候补生）[worker_name:" + "＿" * 15 + HALF * 6 + "]（以下简称“乙方”），",
    ],
}


# --- 第1-6号（日中併記。旧い版。2026-10-20 に人が A を選んだ。ADR-0027 の追記）----------------------
# 日本語の行は、日本語の版の SPEC_1_6 の読点「、」を「，」にしたもの（併記の版の書き方）。合わない行は
# JA_1_6 で置き換える（空のリストは、併記の版に無い行。Ⅸ４ 待遇の相違の窓口は補足の頁に書く）。
# 中国語の行は ZH_1_6 で、日本語の行の後ろに足す。チェックの欄は日本語の行と同じ名前のタグ、ほかの欄は
# 名前の後ろに「_zh」を付けたタグにし、中国語の行の欄の幅で値を詰める（データのキーは同じ）。
# 印刷済みの選択肢の文を値で置き換える欄（更新上限・無期転換の変更・祝日・非定例日・締切日・支払日。
# TRANSLATED_1_6）は、中国語の行では値を T43 の値の訳の表（services/data/translations/zh-hans.json）で
# 中国語にして入れる（マッピングの translate。訳の無い値は日本語のまま入れ、生成前の検査で警告する）。


def full(n: int) -> str:
    return SP * n


def half(n: int) -> str:
    return HALF * n


ARTICLES_ZH_3 = "第＿＿条～第＿＿条、第＿＿条～第＿＿条、第＿＿条～第＿＿条"
ARTICLES_ZH_2 = "第＿＿条～第＿＿条、第＿＿条～第＿＿条"
SHIFT_ZH = (
    "＿＿＿上班（[shift{n}_start_h:2]点[shift{n}_start_mi:2]分）＿下班 （[shift{n}_end_h:2]点"
    "[shift{n}_end_mi:2]分）＿（适用日[shift{n}_applies:5] ，每天规定的劳动时间[shift{n}_daily_h:3]"
    "小时[shift{n}_daily_mi:2]分钟）"
)
ALLOWANCE_ZH = (
    "＿＿({x}) （[allowance_{x}_name:5]津贴[allowance_{x}_amount:5]日元/计算方法："
    "[allowance_{x}_method:＿＿＿＿＿＿＿＿ ＿]）"
)
ALLOWANCE_JA = "{lead}({x}) （[allowance_{x}_name:5]手当[allowance_{x}_amount:5]円／計算方法：[allowance_{x}_method:10]）"
INSURANCE_JA = (
    "＿１．社会保険の加入状況・労働保険の適用状況（[insurance_employees_pension:□]＿厚生年金＿，"
    "[insurance_health:□]＿健康保険＿，[insurance_employment:□]＿雇用保険＿，"
    "[insurance_workers_compensation:□]＿労災保険＿，[insurance_national_pension:□]＿国民年金＿，"
    "[insurance_national_health:□]＿国民健康保険＿，[insurance_other:□]＿その他（[insurance_other_text:6]））"
)
INSURANCE_ZH = (
    "＿1． 社会保险参保情况、劳动保险适用情况（[insurance_employees_pension:□]＿厚生年金＿，"
    "[insurance_health:□]＿健康保险＿，[insurance_employment:□]＿雇佣保险＿，"
    "[insurance_workers_compensation:□]＿工伤保险＿，[insurance_national_pension:□]＿国民年金＿，"
    "[insurance_national_health:□]＿国民健康保险＿，[insurance_other:□]＿其他（[insurance_other_text:6]））"
)

# 日本語の版の SPEC_1_6 の番号 → 併記の版の日本語の行（と、同じ段落の中国語）
JA_1_6: dict[int, list[str]] = {
    0: ["[signed_y:]年[signed_m:3]月[signed_d:3]日"],
    6: [
        "（[starts_y:4]年[starts_m:2]月[starts_d:2]日＿～＿[ends_y:4]年[ends_m:2]月[ends_d:2]日）"
        "＿＿＿＿＿＿＿＿入国予定日[entry_y:4]年[entry_m:2]月[entry_d:2]日"
    ],
    7: [
        "＿＿＿[renewal_automatic:□]＿自動的に更新する＿＿＿＿＿[renewal_possible:□]＿更新する場合があり得る"
        "＿＿＿[renewal_none:□]＿契約の更新はしない"
    ],
    9: [
        "[criterion_business:□]＿会社の経営状況＿＿＿＿＿＿＿＿[criterion_progress:□]＿従事している業務の"
        "進捗状況＿＿＿[criterion_other:□]＿その他（[criterion_other_text:12]）"
    ],
    11: [
        "…末日の翌日（ [conversion_y:2]年 [conversion_m:2]月 [conversion_d:2]日）から、無期雇用契約での雇用に"
        "転換することができる。この場合の本契約からの労働条件の変更の有無（[conversion_change: 無 ・ 有"
        "（別紙２のとおり）]）"
    ],
    12: ["[direct_employment:□]＿直接雇用（以下に記入）＿[direct_employment:□]＿直接雇佣（填写以下内容）"],
    13: ["事業所名＿[site_name:19]事业所名称＿[site_name_zh:19]"],
    14: [
        f"所在地＿[site_address:19]所{half(2)}在{half(2)}地{half(2)}[site_address_zh:19]",
        "連絡先＿[site_phone:19]",
        f"联系方式＿{half(2)}[site_phone_zh:19]",
    ],
    16: ["事業所名＿[workplace_change:19]事业所名称＿[workplace_change_zh:19]"],
    17: [
        "１．分＿＿野（[field:11]）",
        f"1． 领{half(3)}域（[field_zh:11]）",
        "＿２．業務区分（[categories:11]）",
        "２．业务区分（[categories_zh:11]）",
    ],
    18: [
        "（変更の範囲）[duties_unchanged:□]変更の可能性なし（変更ある場合は以下に記入）"
        "（变更的范围）[duties_unchanged:□]没有变更的可能性（有变更时填写以下内容）"
    ],
    55: [INSURANCE_JA, INSURANCE_ZH],
    56: [],
    59: [],  # Ⅸ４ 待遇の相違の窓口は併記の版に無い（補足の頁）
    61: ["以上のほかは、当社就業規則による。就業規則を確認できる場所や方法（[work_rules_location:25]）"],
    64: ["※日給・時給の場合の１か月当たりの金額（[monthly_equivalent:6]円）"],
    65: [ALLOWANCE_JA.format(lead="", x="a")],
    68: [ALLOWANCE_JA.format(lead=" ", x="d")],
    70: ["＿・支給要件：時間外労働の有無にかかわらず、[fixed_overtime_hours:5]時間分の時間外手当として支給。"],
    75: ["(c) 雇用保険料＿＿＿＿＿  （約[deduction_employment_insurance:6]円）"],
    76: ["(d) 食＿＿＿費＿＿＿＿ ＿ （約[deduction_food:6]円）"],
    78: ["(f) その他 （水道光熱費） （約[deduction_utilities:6]円）"],
}

# 日本語の版の SPEC_1_6 の番号 → その後ろに置く中国語の行
ZH_1_6: dict[int, list[str]] = {
    0: ["[signed_y:]年[signed_m:3]月[signed_d:3]日"],
    1: ["[worker_name:15]先生/女士"],
    2: ["特定技能所属机构名称[host_name:" + "_" * 34 + half(22) + "]"],
    3: ["地址" + half(17) + "[host_address:" + "_" * 34 + "]"],
    4: ["电话号码" + half(12) + "[host_phone:" + "_" * 34 + "]"],
    5: [f"代表人{half(3)}职务、姓名{half(2)}[host_representative:" + "_" * 27 + "]（盖章）"],
    6: [
        "＿＿（[starts_y:4]年[starts_m:2]月[starts_d:2]日＿～＿[ends_y:4]年[ends_m:2]月[ends_d:2]日）"
        "＿＿＿＿＿＿＿＿预定入境日期[entry_y:3]年[entry_m:2]月[entry_d:2]日"
    ],
    7: [
        f"[renewal_automatic:□]＿自动更新＿＿＿＿{half(9)}＿[renewal_possible:□]＿可能更新{half(15)}"
        "＿＿＿[renewal_none:□]＿不更新合同"
    ],
    8: [
        f"[criterion_workload:□]＿合同到期时的业务量{half(4)}＿＿＿＿[criterion_performance:□]＿劳动者的工作"
        "成绩、态度 ＿＿ ＿＿[criterion_ability:□]＿劳动者履行业务的能力"
    ],
    9: [
        "[criterion_business:□]＿公司的经营情况＿＿＿＿＿＿＿＿[criterion_progress:□]＿从事的业务的进度情况"
        f"{half(7)}＿＿＿[criterion_other:□]＿其他（[criterion_other_text:13]）"
    ],
    10: ["３． 有无更新上限（[renewal_limit:无・有（最多更新＿＿次／总合同期限＿＿年为止）]）"],
    11: [
        "…从本合同期限的最后一天的第二天([conversion_y:  ]年 [conversion_m:]月 [conversion_d:]日)开始，"
        "可以转换为无期雇佣合同的雇佣。在这种情况下，有无根据本合同的劳动条件变更（[conversion_change: 无/有"
        "（如附件2所示）]）"
    ],
    15: ["（变更的范围）[workplace_unchanged:□]没有变更的可能性（有变更时填写以下内容）"],
    19: [f"1． 领{half(3)}域（[duty_change:11]）"],
    20: [
        "(1) 上班＿＿（[start_h:2]点[start_mi:2]分）＿＿＿下班＿＿（[end_h:2]点[end_mi:2]分）＿＿＿"
        "（每天规定的劳动时间[daily_h:3]小时[daily_mi:2]分钟）"
    ],
    21: ["＿＿[variable_hours:□]＿非常规劳动时间制：（[variable_hours_unit:4]）单位非常规劳动时间制"],
    22: ["＿＿[shifts:□]＿根据下述工作时间的调配，采用轮班制。"],
    23: [SHIFT_ZH.format(n=1)],
    24: [SHIFT_ZH.format(n=2)],
    25: [SHIFT_ZH.format(n=3)],
    26: [f"{half(3)}2. 休息时间{half(5)}（[break_minutes:{half(6)}]分钟）"],
    27: [
        f"＿ 3．规定劳动时间＿{half(2)}①每周（[weekly_h:2]小时[weekly_mi:3]分钟）＿②每月（[monthly_h:2]小时"
        "[monthly_mi:3]钟）＿③每年（[annual_h:3]小时[annual_mi:2]分钟）"
    ],
    28: [
        f"＿ 4．规定劳动天数＿{half(2)}①每周（[weekly_days:＿＿ ＿]天）＿＿＿＿＿ ②每月（[monthly_days:3]天）"
        "＿＿＿＿＿③每年（[annual_days:3]天 ）"
    ],
    29: [f"＿ 5．有无规定时间以外劳动＿＿{half(2)}[overtime_yes:□]＿有＿＿＿＿[overtime_no:□]＿无"],
    30: [f"○详细内容参见就业规则＿[articles_hours:{ARTICLES_ZH_3}]"],
    31: [
        "1．规定休息日：每周[holiday_weekdays:周＿＿＿]、[national_holidays:日本的法定节日]、其他（[holidays_other:"
        f"{full(9)}{half(3)}{full(2)}]）＿＿（全年合计休息日天数[annual_holidays:3]天）"
    ],
    32: [f"2.非规定休息日：[irregular_holidays:每周/每月{half(8)}天，其他（＿＿＿＿＿＿＿＿＿＿＿）]"],
    33: [f"○详细内容参见就业规则[articles_holidays:{ARTICLES_ZH_2}]"],
    34: [f"1．年度带薪休假＿＿＿连续工作满6个月的→[paid_leave_days:{full(3)}{half(6)}{full(1)}]天"],
    35: [
        f"{full(12)}连续工作未满6个月时的年度带薪休假（[early_leave_yes:□]＿有＿＿＿ [early_leave_no:□]＿无）"
        "＿→ 经过[early_leave_months:3]个月者[early_leave_days:3]天"
    ],
    36: [
        f"2．其他休假＿＿＿{half(5)}带薪休假（[other_paid_leave:9]）无薪休假（[other_unpaid_leave:11]）"
    ],
    37: [f"○详细内容参见就业规则[articles_leave:{ARTICLES_ZH_2}]"],
    38: [
        "1． 基本工资＿＿＿[wage_monthly:□]＿月薪（[wage_monthly_amount:7]日元）＿[wage_daily:□]＿日薪"
        f"（[wage_daily_amount:7]日元）＿[wage_hourly:□]＿时薪（[wage_hourly_amount:{full(6)}{half(2)}{full(1)}]"
        "日元）"
    ],
    39: ["＿＿＿＿＿（[allowance1:7]津贴、[allowance2:7]津贴、[allowance3:7]津贴）"],
    40: ["＿＿(1) 规定时间外＿＿每月超过法定60小时以内（[rate_overtime_within_60h:5]）％"],
    41: [f"{full(11)}每月超过法定60小时以上（[rate_overtime_over_60h:5]）％"],
    42: [f"{full(11)}超过法定{half(12)}＿{half(3)}（[rate_overtime_scheduled:5]）％"],
    43: [
        "(2) 休息日＿＿＿ 法定休息日＿＿ （[rate_holiday_statutory:5]）％，＿"
        f"{half(2)}非法定休息日＿＿（[rate_holiday_non_statutory:5]）％"
    ],
    44: ["(3) 深夜＿＿＿＿＿（[rate_night:5]）％"],
    45: ["4．工资结算日＿＿＿[wage_closing_day:□＿毎月＿＿＿日、＿□＿毎月＿＿＿日]"],
    46: ["5．工资支付日＿＿＿[wage_payment_day:□＿毎月＿＿＿日、＿□＿毎月＿＿＿日]"],
    47: ["6．工资支付方法＿＿[payment_bank:□]＿汇入银行账户＿＿[payment_cash:□]＿法定货币支付"],
    48: [
        f"＿ 7．根据劳资合同在支付工资时的扣除＿＿{half(2)}[deduction_agreement_no:□]{half(2)}无＿＿ ＿＿"
        "[deduction_agreement_yes:□]＿有"
    ],
    49: ["8．加薪＿＿＿＿＿＿[raise_yes:□]＿有（时间、金额等[raise_note:24]），＿[raise_no:□]＿无"],
    50: ["＿ 9．奖金＿＿＿＿＿＿[bonus_yes:□]＿有（时间、金额等[bonus_note:24]），＿[bonus_no:□]＿无"],
    51: [
        f"10．退职金＿＿＿＿{half(2)}[retirement_yes:□]＿有（时间、金额等[retirement_note:24]），＿"
        "[retirement_no:□]＿无"
    ],
    52: [
        "11．停业津贴＿＿＿＿[leave_allowance:□]＿有（比例[leave_allowance_rate:"
        f"{full(17)}{half(8)}{full(7)}]）"
    ],
    53: ["1．因个人原因离职时的手续（离职的[resignation_notice_days:4]天前报告社长、工厂长等）"],
    54: [f"○详细内容参见就业规则[articles_resignation:{ARTICLES_ZH_2}]"],
    57: [f"2． 雇佣时的体检{full(6)}{half(7)}[hiring_check_y:2]年[hiring_check_m:5]月"],
    58: [
        f"＿3． 首次定期体检＿＿＿＿[first_check_y:{full(4)}{half(7)}]年[first_check_m:5]月＿（以后每"
        "[check_interval:5]实施）"
    ],
    60: ["部门名称[consultation_department:7]负责人职务和姓名[consultation_person:8]（联系方式[consultation_phone:9]）"],
    61: ["除上述规定外，适用本公司就业规则。可确认就业规则的场所和方法（[work_rules_location:23]）"],
    62: [
        "＿＿[annex_wage_monthly:□]＿月薪（[annex_wage_monthly_amount:4]日元）＿[annex_wage_daily:□]＿日薪"
        "（[annex_wage_daily_amount:4]日元）＿[annex_wage_hourly:□]＿时薪（[annex_wage_hourly_amount:5]日元）"
    ],
    63: ["※采用月薪/日薪制时，折算时薪为（[hourly_equivalent:6]日元）"],
    64: ["※采用日薪/时薪制时，折算月薪为（[monthly_equivalent:6]日元）"],
    65: [ALLOWANCE_ZH.format(x="a")],
    66: [ALLOWANCE_ZH.format(x="b")],
    67: [ALLOWANCE_ZH.format(x="c")],
    68: [ALLOWANCE_ZH.format(x="d")],
    71: [
        "(e) （[fixed_overtime_name:5]津贴[fixed_overtime_amount:5]日元",
        "＿・支付条件：不论是否有规定时间外劳动，都按[fixed_overtime_hours:5]小时支付加班津贴。",
        "对于超过[fixed_overtime_hours_over:6]小时的规定时间外劳动部分，追加支付增加工资。）",
    ],
    72: [
        f"3．每个月支付的大致金额（1+2）{full(6)}{half(2)}＿{half(6)}＿＿约[monthly_total:8]日元（总计）"
    ],
    73: [f"＿＿(a) 税{half(6)}款＿＿＿＿＿{half(2)}（约[deduction_tax:6]日元）"],
    74: [f"(b) 社会保险费＿＿＿＿＿{half(2)}（约[deduction_social_insurance:6]日元）"],
    75: [f"(c) 雇佣保险费＿＿＿＿＿{half(2)}（约[deduction_employment_insurance:6]日元）"],
    76: [f"{half(4)}(d) 餐{half(6)}费＿＿＿＿ ＿ （约[deduction_food:6]日元）"],
    77: [f"(e) 住{half(2)}宿{half(2)}费＿＿＿＿＿{half(2)}（约[deduction_housing:6]日元）"],
    78: [f"(f) 其{half(2)}他 （水电燃气费） （约[deduction_utilities:6]日元）"],
    **{
        78 + n: [f"＿＿＿＿＿＿ ＿（[deduction_other{n}_name:5]） （约[deduction_other{n}_amount:6]日元）"]
        for n in range(1, 6)
    },
    84: ["＿＿扣除金额＿＿约[deduction_total:8]日元（总计）"],
    85: [f"5．到手金额（3-4）{full(8)}{half(7)}＿＿{half(3)}约[take_home:8]日元（总计）"],
}

# 値を入れると行の終わりの「）」が次の行に折り返す欄（併記の版は行の幅いっぱいに空白を置いている。
# 空白は行の外にはみ出せるが字ははみ出せない）。詰める幅を狭める
PADS_1_6 = {"work_rules_location": 22, "annual_holidays": 3}

# 中国語の行で、値を訳の表で中国語にして入れる欄（印刷済みの選択肢の文を置き換える欄）
TRANSLATED_1_6 = {
    "holiday_weekdays",  # 曜日（「土・日」→「六、日」。印刷済みの「周」も欄に含めて「每周六、日」にする）
    "renewal_limit",
    "conversion_change",
    "national_holidays",
    "irregular_holidays",
    "wage_closing_day",
    "wage_payment_day",
}

# 補足の頁（自作。tools/forms/make_zh_supplement.py）に置く欄: Ⅸ４ 待遇の相違の窓口
SUPPLEMENT_1_6 = [
    {"name": "treatment_department", "original": full(7)},
    {"name": "treatment_person", "original": full(8)},
    {"name": "treatment_phone", "original": full(9)},
]


ZH_SLOT = re.compile(r"\[([a-z0-9_]+):(?!□\])")


def spec_1_6() -> list[str]:
    spec: list[str] = []
    for index, markup in enumerate(SPEC_1_6):
        spec.extend(JA_1_6.get(index, [markup.replace("、", "，")]))
        spec.extend(ZH_SLOT.sub(r"[\1_zh:", line) for line in ZH_1_6.get(index, []))
    return spec


def make_1_6(document: docx_tags.Document) -> list[dict]:
    annex2 = document.find("参考様式第１－６号" + SP + "別紙２")
    slots = apply(document, spec_1_6(), stop=annex2)
    # 別紙２ は生成では出さない（日本語の版と同じ。T34-D3）
    last = docx_tags.paragraph_spans(document.xml)[-1][1]
    document.insert_paragraph_after_position(last, "{%p endif %}")
    document.insert_paragraph_before(annex2, "{%p if annex2 %}")
    unique: dict[str, dict] = {}
    for slot in [*slots, *SUPPLEMENT_1_6]:
        unique.setdefault(slot["name"], slot)  # 同じ名前のタグ（チェック・日付の年）は先の行
    items = []
    for slot in unique.values():
        zh = slot["name"].endswith("_zh")
        base = {"name": slot["name"][: -len("_zh")] if zh else slot["name"], "original": slot["original"]}
        item = fields_of("c", [base], TEXT_FIELDS_1_6, LABELS_1_6, YEN_FIELDS_1_6)[0]
        if zh:
            item["target"] = slot["name"]
            item["label"] += "（中国語の行）"
            if base["name"] in TRANSLATED_1_6:
                item["translate"] = "zh-hans"
        if item["target"] in PADS_1_6:
            item["pad"] = PADS_1_6[item["target"]]
        items.append(item)
    return items


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
    if form_id == "1-6":
        return make_1_6(document)  # 中国語の行は spec_1_6 の中で置く
    if form_id == "1-5":
        items = fields_of("c", apply(document, SPEC_1_5), TEXT_FIELDS_1_5, LABELS_1_5, set())
    else:
        items = make_briefing(document)
    apply(document, SPEC_ZH[form_id])  # 中国語の行（同じ名前のタグ。マッピングの欄は増えない）
    return items


NOTES = {
    "1-6": "日中併記の旧い版（入管の 001339744.docx の第1-6号。2026-10-20 に人が A を選んだ。ADR-0027 の追記）。欄とデータのキーは日本語の版（T34、ADR-0023）と同じ。今の様式との差（Ⅸ４ 待遇の相違の窓口など）は補足の頁（自作の日中併記。supplement.docx）に書き、Ⅸ４ の 3 欄は補足の頁に入れる。値は日本語の行と、中国語の行のチェック・数・名前・自由記述の欄に入れる。別紙２ は生成では出さない（T34-D3）。",
    "1-5": "T38 の日中併記の版（ADR-0027）。欄とデータのキーは日本語の版（T34、ADR-0023）と同じ。甲・乙の名前は中国語の行にも入れる（修正 1 回目 A7）。乙の署名は手で書く。",
    "5-8": "T38 の日中併記の版（ADR-0027）。欄とデータのキーは日本語の版（T32、ADR-0021）と同じ。値は日本語の行と中国語の行の両方に入れる（修正 1 回目 A7）。",
    "5-9": "T38 の日中併記の版（ADR-0027）。欄とデータのキーは日本語の版（T32、ADR-0021）と同じ。値は日本語の行と中国語の行の両方に入れる（修正 1 回目 A7）。",
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
        raise SystemExit("usage: make_zh_templates.py <1-5|1-6|5-8|5-9> <folder>")
    main(sys.argv[1], sys.argv[2])
