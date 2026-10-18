"""雇用契約の変更届出の要否（運用要領 令和8年8月版 第7章 別表）。DB を使わない。"""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from services.filing_rules import contract_change_findings

BASE: dict[str, Any] = {
    "starts_on": date(2026, 4, 1),
    "ends_on": date(2027, 3, 31),
    "renewal": "possible",
    "renewal_criteria": ["workload"],
    "renewal_criteria_other": "",
    "has_renewal_limit": False,
    "renewal_limit_count": None,
    "renewal_limit_years": None,
    "site": "テスト工業 本社工場",
    "workplace_change_scope": "",
    "categories": ["機械金属加工"],
    "duties": "機械加工",
    "duty_change_scope": "",
    "start_time": "08:30",
    "end_time": "17:30",
    "daily_working_minutes": 480,
    "variable_hours_unit": "",
    "weekly_working_minutes": 2400,
    "monthly_working_minutes": None,
    "annual_working_minutes": None,
    "weekly_working_days": Decimal("5"),
    "monthly_working_days": None,
    "annual_working_days": 245,
    "has_overtime": True,
    "regular_holiday_weekdays": ["sat", "sun"],
    "annual_holidays": 120,
    "paid_leave_days": 10,
    "has_early_paid_leave": True,
    "early_paid_leave_months": 3,
    "early_paid_leave_days": 3,
    "other_paid_leave": "慶弔休暇",
    "other_unpaid_leave": "",
    "wage_type": "monthly",
    "base_wage": 220000,
    "overtime_rate_within_60h": Decimal("25"),
    "night_rate": Decimal("25"),
    "leave_allowance_rate": Decimal("60"),
    "payment_method": "bank_transfer",
    "has_deduction_agreement": True,
    "has_raise": True,
    "raise_note": "年1回",
    "has_bonus": True,
    "bonus_note": "年2回",
    "has_retirement_allowance": False,
    "retirement_allowance_note": "",
    "resignation_notice_days": 30,
    "insurances": ["employees_pension", "health", "employment", "workers_compensation"],
    "periodic_health_check_interval": "1年",
    "allowances": [("住宅手当", 10000, "定額", None)],
    "deductions": [("housing", "", 30000)],
}


def _codes(changes: dict[str, Any]) -> tuple[set[str], set[str]]:
    findings = contract_change_findings(BASE, BASE | changes)
    return {f.code for f in findings.required}, {f.code for f in findings.review}


def test_no_change_no_filing() -> None:
    assert not contract_change_findings(BASE, dict(BASE)).needs_filing


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"ends_on": date(2026, 12, 31)}, "period_shortened"),
        ({"renewal": "none"}, "renewal_changed"),
        ({"renewal_criteria": ["workload", "ability"]}, "renewal_criteria_changed"),
        ({"site": "テスト工業 第二工場"}, "site_changed"),
        ({"categories": ["縫製"]}, "categories_changed"),
        ({"variable_hours_unit": "one_year"}, "one_year_variable_hours"),
        ({"weekly_working_minutes": 2280}, "scheduled_hours_changed"),
        ({"annual_working_days": 250}, "scheduled_hours_changed"),
        ({"annual_holidays": 110}, "annual_holidays_decreased"),
        ({"paid_leave_days": 8}, "paid_leave_decreased"),
        ({"has_early_paid_leave": False}, "early_paid_leave_abolished"),
        ({"other_paid_leave": ""}, "other_paid_leave_abolished"),
        ({"wage_type": "hourly", "base_wage": 1300}, "wage_type_changed"),
        ({"base_wage": 210000}, "base_wage_decreased"),
        ({"allowances": []}, "allowance_abolished"),
        ({"allowances": [("住宅手当", 8000, "定額", None)]}, "allowance_decreased"),
        (
            {
                "allowances": [
                    ("住宅手当", 10000, "定額", None),
                    ("固定残業手当", 20000, "", Decimal("10")),
                ]
            },
            "fixed_overtime_changed",
        ),
        ({"night_rate": Decimal("20")}, "premium_rate_decreased"),
        ({"leave_allowance_rate": None}, "premium_rate_decreased"),
        ({"payment_method": "cash"}, "payment_to_cash"),
        ({"has_deduction_agreement": False}, "deduction_agreement_changed"),
        ({"deductions": [("housing", "", 30000), ("food", "", 15000)]}, "deduction_added"),
        ({"has_bonus": False}, "has_bonus_abolished"),
        ({"resignation_notice_days": 14}, "resignation_changed"),
        ({"insurances": ["employment", "workers_compensation"]}, "social_insurance_changed"),
    ],
)
def test_changes_requiring_filing(changes: dict[str, Any], code: str) -> None:
    required, _review = _codes(changes)
    assert code in required


@pytest.mark.parametrize(
    "changes",
    [
        {"base_wage": 230000},  # 昇給
        {"allowances": [("住宅手当", 12000, "定額", None)]},  # 手当の増額
        {
            "allowances": [("住宅手当", 10000, "定額", None), ("皆勤手当", 5000, "", None)]
        },  # 手当の追加
        {"start_time": "09:00", "end_time": "18:00"},  # 始業・終業だけ
        {"ends_on": date(2028, 3, 31)},  # 期間の延長
        {"renewal": "automatic"},  # 「更新する場合があり得る」→「自動的に更新する」
        {"annual_holidays": 125},  # 休日を増やす
        {"paid_leave_days": 12},  # 休暇を増やす
        {"night_rate": Decimal("30")},  # 割増率を上げる
        {"variable_hours_unit": "one_month"},  # 1年単位以外の変形労働時間制（令和8年8月版）
        {"annual_working_days": 240},  # 年間所定労働日数を少なくする（令和8年8月版で届出不要）
        {"deductions": [("housing", "", 25000)]},  # 控除額の減少
        {"payment_method": "bank_transfer"},
        {"has_retirement_allowance": True, "retirement_allowance_note": "勤続3年以上"},  # 新設
        {"treatment_explanation_contact": "総務部"},  # 雇用条件に影響しない項目
        {"consultation_contact_department": "人事部", "consultation_contact_phone": "03"},  # T34
    ],
)
def test_changes_not_requiring_filing(changes: dict[str, Any]) -> None:
    assert not contract_change_findings(BASE, BASE | changes).needs_filing


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"duties": "機械加工と検査"}, "duties_changed"),
        ({"deductions": []}, "deduction_removed"),
        ({"deductions": [("housing", "", 35000)]}, "deduction_increased"),
        ({"bonus_note": "年1回"}, "bonus_note_changed"),
        ({"periodic_health_check_interval": "2年"}, "health_check_interval_changed"),
        ({"other_paid_leave": "慶弔休暇（3日）"}, "other_paid_leave_changed"),
        ({"has_renewal_limit": True, "renewal_limit_count": 2}, "renewal_limit_changed"),
    ],
)
def test_changes_needing_review(changes: dict[str, Any], code: str) -> None:
    required, review = _codes(changes)
    assert code in review
    assert required == set()


def test_from_none_or_possible_to_automatic_renewal_is_exempt_but_other_way_is_not() -> None:
    assert not contract_change_findings(
        BASE | {"renewal": "none"}, BASE | {"renewal": "automatic"}
    ).needs_filing
    assert _codes({"renewal": "none"})[0] == {"renewal_changed"}
    assert contract_change_findings(
        BASE | {"renewal": "automatic"}, BASE | {"renewal": "possible"}
    ).required
