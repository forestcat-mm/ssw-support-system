"""雇用契約の書類（第1-6号 雇用条件書・第1-5号 雇用契約書・第1-4号 報酬に関する説明書）のデータを、
雇用契約の版から作る（T34、ADR-0023）。

欄の名前（キー）は tools/forms/make_contract_templates.py の欄の名前に「c.」（第1-4号は「w.」）を
付けたもの。数字は全角にし、欄の幅にそろえる（マッピング定義の format と pad。T34-D6）。
- 冒頭の日付・締結日は版の締結日（T34-D5。空なら空欄にして警告）。
- □ は ■（選んだもの）と □（選ばなかったもの）。印刷済みの「毎月　日」・就業規則の条・非定例日は
  値で置き換える（T34-D4）。国民の祝日が休日でない版は「日本の国民の祝日」を全角空白で消す（T34-D8）。
- 手当・控除・交代制は様式の行までを書き、合計は全部の行から計算する（T34-D7。超えた行は警告）。
- 窓口（Ⅸ４・５）は版の 3 つの欄（T34 から）。古い版は部署名の欄に 1 つの文を書く（警告）。
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Iterable
from typing import Any

from apps.contracts.models import (
    ContractWageExplanation,
    ConversionChange,
    DeductionKind,
    EmploymentContractAllowance,
    EmploymentContractDeduction,
    EmploymentContractShift,
    EmploymentContractVersion,
    Insurance,
    Renewal,
    RenewalCriterion,
    VariableHoursUnit,
    WageComparison,
    WageType,
    WageUnit,
)
from apps.workers.models import Sex
from services import contracts, wage_explanations

SP = "　"
FORM_SHIFTS = 3
FORM_ALLOWANCE_NAMES = 3  # 本体 Ⅶ２ の手当の名前
FORM_ALLOWANCES = 4  # 別紙１ ２(a)〜(d)
FORM_FIXED_OVERTIME = 1  # 別紙１ ２(e)
FORM_OTHER_DEDUCTIONS = 5  # 別紙１ ４ の名前を書く行
FIXED_DEDUCTIONS: dict[str, str] = {
    DeductionKind.TAX: "tax",
    DeductionKind.SOCIAL_INSURANCE: "social_insurance",
    DeductionKind.EMPLOYMENT_INSURANCE: "employment_insurance",
    DeductionKind.FOOD: "food",
    DeductionKind.HOUSING: "housing",
    DeductionKind.UTILITIES: "utilities",
}
WEEKDAY_WORDS = {
    "mon": "月",
    "tue": "火",
    "wed": "水",
    "thu": "木",
    "fri": "金",
    "sat": "土",
    "sun": "日",
}
VARIABLE_UNITS: dict[str, str] = {
    VariableHoursUnit.ONE_WEEK: "１週間",
    VariableHoursUnit.ONE_MONTH: "１か月",
    VariableHoursUnit.ONE_YEAR: "１年",
}
INSURANCE_KEYS: dict[str, str] = {
    Insurance.EMPLOYEES_PENSION: "employees_pension",
    Insurance.HEALTH: "health",
    Insurance.EMPLOYMENT: "employment",
    Insurance.WORKERS_COMPENSATION: "workers_compensation",
    Insurance.NATIONAL_PENSION: "national_pension",
    Insurance.NATIONAL_HEALTH: "national_health",
    Insurance.OTHER: "other",
}
CRITERIA_KEYS: dict[str, str] = {
    RenewalCriterion.WORKLOAD: "workload",
    RenewalCriterion.PERFORMANCE: "performance",
    RenewalCriterion.ABILITY: "ability",
    RenewalCriterion.BUSINESS: "business",
    RenewalCriterion.PROGRESS: "progress",
    RenewalCriterion.OTHER: "other",
}
FULL_WIDTH = str.maketrans("0123456789", "０１２３４５６７８９")


def _ymd(prefix: str, day: datetime.date | None) -> dict[str, Any]:
    if day is None:
        return {}
    return {f"{prefix}_y": day.year, f"{prefix}_m": day.month, f"{prefix}_d": day.day}


def _hm(prefix: str, minutes: int | None) -> dict[str, Any]:
    if minutes is None:
        return {}
    hours, rest = divmod(minutes, 60)
    return {f"{prefix}_h": hours, f"{prefix}_mi": rest}


def _time(prefix: str, value: datetime.time) -> dict[str, Any]:
    return {f"{prefix}_h": value.hour, f"{prefix}_mi": value.minute}


def _yes_no(prefix: str, value: bool) -> dict[str, Any]:
    return {f"{prefix}_yes": value, f"{prefix}_no": not value}


def _without_suffix(name: str, suffix: str) -> str:
    """印刷済みの語（「手当」など）と重ならないように、名前の末尾から除く。"""
    return name[: -len(suffix)] if name.endswith(suffix) and name != suffix else name


def _interval(text: str) -> str:
    """「1年ごと」→「1年」（様式は「（その後　ごとに実施）」）。"""
    for suffix in ("ごとに実施", "ごとに", "ごと", "毎"):
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def representative(host: Any) -> str:
    return SP.join(part for part in (host.representative_title, host.representative_name) if part)


def regular_allowances(version: EmploymentContractVersion) -> list[EmploymentContractAllowance]:
    return [a for a in version.allowances.order_by("pk") if a.fixed_overtime_hours is None]


def fixed_overtime_allowances(
    version: EmploymentContractVersion,
) -> list[EmploymentContractAllowance]:
    return [a for a in version.allowances.order_by("pk") if a.fixed_overtime_hours is not None]


def other_deductions(version: EmploymentContractVersion) -> list[EmploymentContractDeduction]:
    return [d for d in version.deductions.order_by("pk") if d.kind not in FIXED_DEDUCTIONS]


def shifts_of(version: EmploymentContractVersion) -> list[EmploymentContractShift]:
    return list(version.shifts.order_by("start_time", "pk"))


def contacts(version: EmploymentContractVersion) -> dict[str, str]:
    """窓口（Ⅸ４・５）の 3 つの欄。T34 より前の版は部署名の欄に 1 つの文を書く（T34-D4）。"""
    values: dict[str, str] = {}
    for prefix, legacy in (
        ("treatment", version.treatment_explanation_contact),
        ("consultation", version.consultation_contact),
    ):
        parts = {
            name: getattr(version, f"{prefix}_contact_{name}")
            for name in ("department", "person", "phone")
        }
        if not any(parts.values()) and legacy.strip():
            parts["department"] = " ".join(legacy.split())
        values |= {f"{prefix}_{name}": value for name, value in parts.items()}
    return values


def legacy_contacts(version: EmploymentContractVersion) -> bool:
    """窓口が T34 より前の 1 つの文だけで入っている（部署名の欄に全文を書く。警告）。"""
    for prefix, legacy in (
        ("treatment", version.treatment_explanation_contact),
        ("consultation", version.consultation_contact),
    ):
        names = ("department", "person", "phone")
        split = any(getattr(version, f"{prefix}_contact_{name}") for name in names)
        if legacy.strip() and not split:
            return True
    return False


def _rows(prefix: str, rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    return {
        f"{prefix}{i}_{name}": value for i, row in enumerate(rows, 1) for name, value in row.items()
    }


# 「毎月◯日」「当月◯日」は印刷済みの「□　毎月　　　日、　□　毎月　　　日」の 1 つ目に入れ、
# ほか（翌月・その他）は印刷済みの文を値で置き換える（T34-D4。修正 1 回目 B4）
MONTHLY_DAY = re.compile(r"^(?:毎月|当月)(末日|(\d{1,2})日)$")


def wage_day_text(value: str) -> str:
    if not value:
        return ""
    found = MONTHLY_DAY.match(value)
    if found is None:
        return f"■{SP}{value}"
    day = "末日" if found.group(2) is None else f"{int(found.group(2))}日"
    return f"■{SP}毎月{day.rjust(4, SP)}、{SP}□{SP}毎月{SP * 3}日"


def build_1_6(version: EmploymentContractVersion) -> dict[str, Any]:
    contract = version.contract
    host = contract.host_organization
    worker = contract.worker
    site = version.site
    early = version.has_early_paid_leave
    data: dict[str, Any] = {
        **_ymd("signed", version.signed_on),
        "worker_name": worker.name_romaji,
        "host_name": host.name,
        "host_address": host.address,
        "host_phone": host.phone_number,
        "host_representative": representative(host),
        **_ymd("starts", version.starts_on),
        **_ymd("ends", version.ends_on),
        **_ymd("entry", version.expected_entry_on),
        "renewal_automatic": version.renewal == Renewal.AUTOMATIC,
        "renewal_possible": version.renewal == Renewal.POSSIBLE,
        "renewal_none": version.renewal == Renewal.NONE,
        "direct_employment": True,
        "site_name": site.name,
        "site_address": site.address,
        "site_phone": site.phone_number,
        "workplace_unchanged": not version.workplace_change_scope.strip(),
        "workplace_change": " ".join(version.workplace_change_scope.split()),
        "field": site.field.name,
        "categories": "、".join(c.name for c in version.categories.order_by("pk")),
        "duties_unchanged": not version.duty_change_scope.strip(),
        "duty_change": " ".join(version.duty_change_scope.split()),
        **_time("start", version.start_time),
        **_time("end", version.end_time),
        **_hm("daily", version.daily_working_minutes),
        "variable_hours": bool(version.variable_hours_unit),
        "variable_hours_unit": VARIABLE_UNITS.get(version.variable_hours_unit, ""),
        "break_minutes": version.break_minutes,
        **_hm("weekly", version.weekly_working_minutes),
        **_hm("monthly", version.monthly_working_minutes),
        **_hm("annual", version.annual_working_minutes),
        "weekly_days": version.weekly_working_days,
        "monthly_days": version.monthly_working_days,
        "annual_days": version.annual_working_days,
        **_yes_no("overtime", version.has_overtime),
        "articles_hours": version.work_rules_articles_hours,
        "articles_holidays": version.work_rules_articles_holidays,
        "articles_leave": version.work_rules_articles_leave,
        "articles_resignation": version.work_rules_articles_resignation,
        "holiday_weekdays": "・".join(
            WEEKDAY_WORDS[day] for day in WEEKDAY_WORDS if day in version.regular_holiday_weekdays
        ),
        # 国民の祝日が休日でなければ、印刷済みの語を全角空白で消す（T34-D8）
        "national_holidays": None if version.national_holidays_off else SP * 8,
        "holidays_other": version.regular_holidays_other,
        "annual_holidays": version.annual_holidays,
        "irregular_holidays": version.irregular_holidays,
        "paid_leave_days": version.paid_leave_days,
        **_yes_no("early_leave", version.has_early_paid_leave),
        "early_leave_months": version.early_paid_leave_months if early else None,
        "early_leave_days": version.early_paid_leave_days if early else None,
        "other_paid_leave": version.other_paid_leave,
        "other_unpaid_leave": version.other_unpaid_leave,
        "rate_overtime_within_60h": version.overtime_rate_within_60h,
        "rate_overtime_over_60h": version.overtime_rate_over_60h,
        "rate_overtime_scheduled": version.overtime_rate_scheduled,
        "rate_holiday_statutory": version.holiday_rate_statutory,
        "rate_holiday_non_statutory": version.holiday_rate_non_statutory,
        "rate_night": version.night_rate,
        # 印刷済みの「□　毎月　日、□　毎月　日」を値で置き換える（T34-D4）
        "wage_closing_day": wage_day_text(version.wage_closing_day),
        "wage_payment_day": wage_day_text(version.wage_payment_day),
        "payment_bank": version.payment_method == "bank_transfer",
        "payment_cash": version.payment_method == "cash",
        "deduction_agreement_no": not version.has_deduction_agreement,
        "deduction_agreement_yes": version.has_deduction_agreement,
        **_yes_no("raise", version.has_raise),
        "raise_note": version.raise_note if version.has_raise else "",
        **_yes_no("bonus", version.has_bonus),
        "bonus_note": version.bonus_note if version.has_bonus else "",
        **_yes_no("retirement", version.has_retirement_allowance),
        "retirement_note": (
            version.retirement_allowance_note if version.has_retirement_allowance else ""
        ),
        "leave_allowance": version.leave_allowance_rate is not None,
        "leave_allowance_rate": (
            f"{version.leave_allowance_rate.normalize():f}％"
            if version.leave_allowance_rate is not None
            else ""
        ),
        "resignation_notice_days": version.resignation_notice_days,
        "insurance_other_text": version.insurance_other,
        **_ymd("hiring_check", version.hiring_health_check_on),
        **_ymd("first_check", version.first_periodic_health_check_on),
        "check_interval": _interval(version.periodic_health_check_interval),
        **contacts(version),
        "work_rules_location": version.work_rules_location,
    }
    for code, key in CRITERIA_KEYS.items():
        data[f"criterion_{key}"] = code in version.renewal_criteria
    data["criterion_other_text"] = (
        version.renewal_criteria_other if RenewalCriterion.OTHER in version.renewal_criteria else ""
    )
    if version.has_renewal_limit:
        count = (
            str(version.renewal_limit_count).translate(FULL_WIDTH)
            if version.renewal_limit_count
            else SP * 2
        )
        years = (
            str(version.renewal_limit_years).translate(FULL_WIDTH)
            if version.renewal_limit_years
            else SP * 2
        )
        data["renewal_limit"] = f"有（更新{count}回まで／通算契約期間{years}年まで）"
    else:
        data["renewal_limit"] = "無"
    if version.indefinite_conversion and version.ends_on is not None:
        data |= _ymd("conversion", version.ends_on + datetime.timedelta(days=1))
        changed = version.conversion_conditions_change == ConversionChange.CHANGED
        data["conversion_change"] = " 有（別紙２のとおり）" if changed else " 無 "
    for code, key in INSURANCE_KEYS.items():
        data[f"insurance_{key}"] = code in version.insurances
    data |= _wages(version)
    data |= _shifts(version)
    data |= _allowances(version)
    data |= _deductions(version)
    return {f"c.{key}": value for key, value in data.items()}


def _wages(version: EmploymentContractVersion) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for kind, key in (
        (WageType.MONTHLY, "monthly"),
        (WageType.DAILY, "daily"),
        (WageType.HOURLY, "hourly"),
    ):
        data[f"wage_{key}"] = version.wage_type == kind
        data[f"wage_{key}_amount"] = version.base_wage if version.wage_type == kind else None
    if version.wage_type in (WageType.MONTHLY, WageType.DAILY):
        data["hourly_equivalent"] = version.hourly_equivalent
    if version.wage_type in (WageType.DAILY, WageType.HOURLY):
        data["monthly_equivalent"] = version.monthly_equivalent
    data["monthly_total"] = contracts.monthly_total(version)
    data["take_home"] = contracts.take_home(version)
    return data


def _shifts(version: EmploymentContractVersion) -> dict[str, Any]:
    rows = shifts_of(version)
    data: dict[str, Any] = {"shifts": bool(rows)}
    for i, shift in enumerate(rows[:FORM_SHIFTS], 1):
        data |= _time(f"shift{i}_start", shift.start_time)
        data |= _time(f"shift{i}_end", shift.end_time)
        data[f"shift{i}_applies"] = shift.applies_to
        data |= _hm(f"shift{i}_daily", shift.daily_working_minutes)
    return data


def _allowances(version: EmploymentContractVersion) -> dict[str, Any]:
    regular = regular_allowances(version)
    data: dict[str, Any] = {}
    for i, allowance in enumerate(regular[:FORM_ALLOWANCE_NAMES], 1):
        data[f"allowance{i}"] = _without_suffix(allowance.name, "手当")
    for letter, allowance in zip("abcd", regular[:FORM_ALLOWANCES], strict=False):
        data[f"allowance_{letter}_name"] = _without_suffix(allowance.name, "手当")
        data[f"allowance_{letter}_amount"] = allowance.amount
        data[f"allowance_{letter}_method"] = allowance.calculation_method
    fixed = fixed_overtime_allowances(version)
    if fixed:
        data["fixed_overtime_name"] = _without_suffix(fixed[0].name, "手当")
        data["fixed_overtime_amount"] = fixed[0].amount
        data["fixed_overtime_hours"] = fixed[0].fixed_overtime_hours
    return data


def _deductions(version: EmploymentContractVersion) -> dict[str, Any]:
    data: dict[str, Any] = {}
    rows = list(version.deductions.order_by("pk"))
    for kind, key in FIXED_DEDUCTIONS.items():
        amounts = [row.amount for row in rows if row.kind == kind]
        data[f"deduction_{key}"] = sum(amounts) if amounts else None
    others = other_deductions(version)
    data |= _rows(
        "deduction_other",
        ({"name": row.name, "amount": row.amount} for row in others[:FORM_OTHER_DEDUCTIONS]),
    )
    data["deduction_total"] = sum(row.amount for row in rows) if rows else None
    return data


def overflowing_rows(version: EmploymentContractVersion) -> list[tuple[str, int, int]]:
    """様式の行を超える明細（欄の名前・行の数・様式の行の数。T34-D7）。"""
    result = []
    for label, count, limit in (
        ("Ⅳ１(2) 交代制の勤務時間", len(shifts_of(version)), FORM_SHIFTS),
        ("Ⅶ２ 諸手当", len(regular_allowances(version)), FORM_ALLOWANCE_NAMES),
        ("別紙１ ２(a)〜(d) 諸手当", len(regular_allowances(version)), FORM_ALLOWANCES),
        ("別紙１ ２(e) 固定残業代", len(fixed_overtime_allowances(version)), FORM_FIXED_OVERTIME),
        ("別紙１ ４ その他の控除", len(other_deductions(version)), FORM_OTHER_DEDUCTIONS),
    ):
        if count > limit:
            result.append((label, count, limit))
    return result


# --- 第1-5号 特定技能雇用契約書 -------------------------------------------------------------


def build_1_5(version: EmploymentContractVersion) -> dict[str, Any]:
    host = version.contract.host_organization
    data = {
        "host_name": host.name,
        "worker_name": version.contract.worker.name_romaji,
        **_ymd("signed", version.signed_on),
        # 甲の欄は名称と代表者を 2 行に分ける（1 行では乙の署名の線を押し出す。ADR-0023）
        "host_signature": "\n".join(
            [host.name, *([SP * 2 + representative(host)] if representative(host) else [])]
        ),
    }
    return {f"c.{key}": value for key, value in data.items()}


# --- 第1-4号 報酬に関する説明書 -------------------------------------------------------------

SEX_WORDS: dict[str, str] = {Sex.MALE: "男", Sex.FEMALE: "女"}


def age_on(birth_date: datetime.date, on: datetime.date) -> int:
    return on.year - birth_date.year - ((on.month, on.day) < (birth_date.month, birth_date.day))


def default_author(version: EmploymentContractVersion) -> str:
    """作成責任者の初期値（受入れ機関の代表者の役職・氏名。生成の画面で変えられる）。"""
    return representative(version.contract.host_organization)


def _wage_by_unit(prefix: str, unit: str, amount: int | None) -> dict[str, Any]:
    if amount is None or not unit:
        return {}
    return {f"{prefix}_{'monthly' if unit == WageUnit.MONTHLY else 'hourly'}": amount}


def build_1_4(
    version: EmploymentContractVersion, basis_on: datetime.date, *, author: str
) -> dict[str, Any]:
    worker = version.contract.worker
    host = version.contract.host_organization
    explanation: ContractWageExplanation | None = wage_explanations.explanation_of(version)
    data: dict[str, Any] = {
        "applicant_name": worker.name_romaji,
        "applicant_age": age_on(worker.birth_date, basis_on),
        "applicant_sex": SEX_WORDS.get(worker.sex, ""),
        **_ymd("made", basis_on),
        "host_name": SP + host.name,
        "author": author,
    }
    if explanation is not None:
        unit = explanation.wage_unit
        data |= {
            "applicant_duties": explanation.applicant_duties,
            "applicant_experience": explanation.applicant_experience_years,
            "applicant_other": explanation.applicant_other,
            **_wage_by_unit("applicant", unit, wage_explanations.applicant_wage(version, unit)),
        }
        prefixes: dict[str, str] = {
            WageComparison.COMPARABLE: "comparable",
            WageComparison.CLOSEST: "closest",
        }
        prefix = prefixes.get(explanation.comparison)
        if prefix is not None:
            data |= {
                f"{prefix}_duties": explanation.japanese_duties,
                f"{prefix}_age": explanation.japanese_age,
                f"{prefix}_sex": SEX_WORDS.get(explanation.japanese_sex, ""),
                f"{prefix}_experience": explanation.japanese_experience_years,
                **_wage_by_unit(prefix, unit, explanation.japanese_wage),
                f"{prefix}_reason": explanation.reason,
                f"{prefix}_other": explanation.other,
            }
            if explanation.has_wage_rules is not None:
                data[f"{prefix}_has_rules"] = "有" if explanation.has_wage_rules else "無"
            if explanation.has_wage_rules:
                data |= _wage_by_unit(f"{prefix}_rules", unit, explanation.rules_wage)
    return {f"w.{key}": value for key, value in data.items()}
