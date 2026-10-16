"""雇用契約の変更が「特定技能雇用契約の変更に係る届出」の対象かを判定する（純粋関数）。

根拠: 特定技能外国人受入れに関する運用要領（令和8年8月20日一部改正版）第7章第1節第1
「別表（特定技能雇用契約の変更関係）」。別表の項番は雇用条件書（参考様式第1-6号）の
Ⅰ〜Ⅸ に対応し、掲げられた変更は具体例であって届出対象はこれに限られない。

- required: 別表で届出が必要とされている変更（起票する）
- review:   機械的に判断できない変更（要否の確認の印を付けて起票する）
- どちらにも当たらない変更（昇給、始業・終業の時刻だけの変更など）では起票しない。

入力は services.contracts.snapshot() の辞書（版の値・業務区分の名称・明細の一覧）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from django.utils.translation import gettext as _

Snapshot = Mapping[str, Any]

ONE_YEAR_UNIT = "one_year"
SOCIAL_INSURANCES = ("health", "employees_pension")
LABOR_INSURANCES = ("employment", "workers_compensation")
PREMIUM_RATES = (
    "overtime_rate_within_60h",
    "overtime_rate_over_60h",
    "overtime_rate_scheduled",
    "holiday_rate_statutory",
    "holiday_rate_non_statutory",
    "night_rate",
    "leave_allowance_rate",
)


@dataclass(frozen=True)
class Finding:
    code: str
    item: str  # 雇用条件書の項番（Ⅰ〜Ⅸ）
    message: str


@dataclass
class ChangeFindings:
    required: list[Finding] = field(default_factory=list)
    review: list[Finding] = field(default_factory=list)

    @property
    def needs_filing(self) -> bool:
        return bool(self.required or self.review)

    def as_details(self) -> dict[str, Any]:
        return {
            "required": [f.__dict__ for f in self.required],
            "review": [f.__dict__ for f in self.review],
        }


def contract_change_findings(old: Snapshot, new: Snapshot) -> ChangeFindings:
    findings = ChangeFindings()
    for rule in RULES:
        rule(old, new, findings)
    return findings


# --- 補助 -----------------------------------------------------------------------


def _changed(old: Snapshot, new: Snapshot, name: str) -> bool:
    return old.get(name) != new.get(name)


def _decreased(old: Any, new: Any) -> bool:
    """減った（新しい値が空になった場合も減ったとみなす）。"""
    if old is None:
        return False
    return new is None or Decimal(str(new)) < Decimal(str(old))


def _increased(old: Any, new: Any) -> bool:
    if new is None:
        return False
    return old is None or Decimal(str(new)) > Decimal(str(old))


def _period_days(starts_on: Any, ends_on: Any) -> int | None:
    if not isinstance(starts_on, date) or not isinstance(ends_on, date):
        return None  # 期間の定めなし
    return (ends_on - starts_on).days


# --- Ⅰ 雇用契約期間 ----------------------------------------------------------------


def _period(old: Snapshot, new: Snapshot, out: ChangeFindings) -> None:
    before = _period_days(old.get("starts_on"), old.get("ends_on"))
    after = _period_days(new.get("starts_on"), new.get("ends_on"))
    if after is not None and (before is None or after < before):
        out.required.append(
            Finding("period_shortened", "Ⅰ", _("The contract period is shortened."))
        )


def _renewal(old: Snapshot, new: Snapshot, out: ChangeFindings) -> None:
    before, after = old.get("renewal"), new.get("renewal")
    if before != after and not (after == "automatic" and before in ("none", "possible")):
        out.required.append(
            Finding("renewal_changed", "Ⅰ", _("Whether the contract is renewed changes."))
        )
    if before == after == "possible" and (
        _changed(old, new, "renewal_criteria") or _changed(old, new, "renewal_criteria_other")
    ):
        out.required.append(
            Finding("renewal_criteria_changed", "Ⅰ", _("The renewal criteria change."))
        )
    if any(
        _changed(old, new, name)
        for name in ("has_renewal_limit", "renewal_limit_count", "renewal_limit_years")
    ):
        out.review.append(Finding("renewal_limit_changed", "Ⅰ", _("The renewal limit changes.")))


# --- Ⅱ 就業の場所 / Ⅲ 業務 ----------------------------------------------------------


def _workplace(old: Snapshot, new: Snapshot, out: ChangeFindings) -> None:
    if _changed(old, new, "site"):
        out.required.append(Finding("site_changed", "Ⅱ", _("The workplace (site) changes.")))
    if _changed(old, new, "workplace_change_scope"):
        out.review.append(
            Finding("workplace_scope_changed", "Ⅱ", _("The possible change of workplace changes."))
        )


def _duties(old: Snapshot, new: Snapshot, out: ChangeFindings) -> None:
    if _changed(old, new, "categories"):
        out.required.append(
            Finding("categories_changed", "Ⅲ", _("The operation category changes."))
        )
    if _changed(old, new, "duties") or _changed(old, new, "duty_change_scope"):
        out.review.append(
            Finding(
                "duties_changed",
                "Ⅲ",
                _("The duties change (filing is required if the worker no longer does SSW work)."),
            )
        )


# --- Ⅳ 労働時間等 ------------------------------------------------------------------


def _working_hours(old: Snapshot, new: Snapshot, out: ChangeFindings) -> None:
    before_unit, after_unit = old.get("variable_hours_unit"), new.get("variable_hours_unit")
    if before_unit != after_unit and ONE_YEAR_UNIT in (before_unit, after_unit):
        out.required.append(
            Finding(
                "one_year_variable_hours",
                "Ⅳ",
                _("The one-year variable working hours system is adopted or abolished."),
            )
        )
    scheduled = (
        "weekly_working_minutes",
        "monthly_working_minutes",
        "annual_working_minutes",
        "weekly_working_days",
        "monthly_working_days",
    )
    changed_hours = any(_changed(old, new, name) for name in scheduled)
    # 年間所定労働日数を当初より少なくする場合は届出不要（令和8年8月版で追記）
    more_annual_days = _increased(old.get("annual_working_days"), new.get("annual_working_days"))
    if changed_hours or (more_annual_days and old.get("annual_working_days") is not None):
        out.required.append(
            Finding("scheduled_hours_changed", "Ⅳ", _("Scheduled working hours or days change."))
        )
    if _changed(old, new, "has_overtime"):
        out.review.append(
            Finding("overtime_changed", "Ⅳ", _("Whether there is overtime work changes."))
        )


# --- Ⅴ 休日 / Ⅵ 休暇 ---------------------------------------------------------------


def _holidays(old: Snapshot, new: Snapshot, out: ChangeFindings) -> None:
    if _decreased(old.get("annual_holidays"), new.get("annual_holidays")):
        out.required.append(
            Finding(
                "annual_holidays_decreased",
                "Ⅴ",
                _(
                    "Annual holidays decrease (not required if only because of leap years, "
                    "calendar or statutory holiday changes)."
                ),
            )
        )


def _leave(old: Snapshot, new: Snapshot, out: ChangeFindings) -> None:
    if _decreased(old.get("paid_leave_days"), new.get("paid_leave_days")):
        out.required.append(Finding("paid_leave_decreased", "Ⅵ", _("Annual paid leave decreases.")))
    if old.get("has_early_paid_leave") and not new.get("has_early_paid_leave"):
        out.required.append(
            Finding(
                "early_paid_leave_abolished", "Ⅵ", _("Paid leave before 6 months is abolished.")
            )
        )
    elif old.get("has_early_paid_leave") and (
        _decreased(old.get("early_paid_leave_days"), new.get("early_paid_leave_days"))
        or _increased(old.get("early_paid_leave_months"), new.get("early_paid_leave_months"))
    ):
        out.required.append(
            Finding("early_paid_leave_decreased", "Ⅵ", _("Paid leave before 6 months decreases."))
        )
    for name in ("other_paid_leave", "other_unpaid_leave"):
        if old.get(name) and not new.get(name):
            out.required.append(Finding(f"{name}_abolished", "Ⅵ", _("Other leave is abolished.")))
        elif old.get(name) and _changed(old, new, name):
            out.review.append(Finding(f"{name}_changed", "Ⅵ", _("Other leave changes.")))


# --- Ⅶ 賃金 ------------------------------------------------------------------------


def _base_wage(old: Snapshot, new: Snapshot, out: ChangeFindings) -> None:
    if _changed(old, new, "wage_type"):
        out.required.append(
            Finding("wage_type_changed", "Ⅶ", _("The way the base wage is paid changes."))
        )
    elif _decreased(old.get("base_wage"), new.get("base_wage")):
        out.required.append(Finding("base_wage_decreased", "Ⅶ", _("The base wage decreases.")))


def _allowances(old: Snapshot, new: Snapshot, out: ChangeFindings) -> None:
    before = {row[0]: row for row in old.get("allowances", [])}
    after = {row[0]: row for row in new.get("allowances", [])}
    if set(before) - set(after):
        out.required.append(Finding("allowance_abolished", "Ⅶ", _("An allowance is abolished.")))
    if any(_decreased(before[name][1], after[name][1]) for name in set(before) & set(after)):
        out.required.append(Finding("allowance_decreased", "Ⅶ", _("An allowance decreases.")))
    had_fixed = any(row[3] is not None for row in before.values())
    has_fixed = any(row[3] is not None for row in after.values())
    if had_fixed != has_fixed:
        out.required.append(
            Finding(
                "fixed_overtime_changed", "Ⅶ", _("Fixed overtime pay is introduced or abolished.")
            )
        )
    if any(before[n][2] != after[n][2] for n in set(before) & set(after)):
        out.review.append(
            Finding(
                "allowance_method_changed",
                "Ⅶ",
                _("The calculation method of an allowance changes."),
            )
        )


def _premiums(old: Snapshot, new: Snapshot, out: ChangeFindings) -> None:
    if any(_decreased(old.get(name), new.get(name)) for name in PREMIUM_RATES):
        out.required.append(
            Finding(
                "premium_rate_decreased",
                "Ⅶ",
                _("A premium wage rate or leave allowance rate decreases."),
            )
        )


def _payment(old: Snapshot, new: Snapshot, out: ChangeFindings) -> None:
    if old.get("payment_method") == "bank_transfer" and new.get("payment_method") == "cash":
        out.required.append(
            Finding(
                "payment_to_cash", "Ⅶ", _("The payment method changes from bank transfer to cash.")
            )
        )
    if _changed(old, new, "has_deduction_agreement"):
        out.required.append(
            Finding(
                "deduction_agreement_changed",
                "Ⅶ",
                _("Whether there are deductions under a labor-management agreement changes."),
            )
        )


def _deductions(old: Snapshot, new: Snapshot, out: ChangeFindings) -> None:
    def key(row: Any) -> tuple[Any, Any]:
        return (row[0], row[1])

    before = {key(row): row for row in old.get("deductions", [])}
    after = {key(row): row for row in new.get("deductions", [])}
    if set(after) - set(before):
        out.required.append(Finding("deduction_added", "Ⅶ", _("A deduction item is added.")))
    if set(before) - set(after):
        out.review.append(
            Finding(
                "deduction_removed",
                "Ⅶ",
                _(
                    "A deduction is abolished "
                    "(filing is required if the worker's own cost increases)."
                ),
            )
        )
    if any(_increased(before[k][2], after[k][2]) for k in set(before) & set(after)):
        out.review.append(Finding("deduction_increased", "Ⅶ", _("A deduction amount increases.")))


def _benefits(old: Snapshot, new: Snapshot, out: ChangeFindings) -> None:
    for flag, note in (
        ("has_raise", "raise_note"),
        ("has_bonus", "bonus_note"),
        ("has_retirement_allowance", "retirement_allowance_note"),
    ):
        if old.get(flag) and not new.get(flag):
            out.required.append(
                Finding(
                    f"{flag}_abolished",
                    "Ⅶ",
                    _("A pay raise, bonus or retirement allowance is abolished."),
                )
            )
        elif old.get(flag) and _changed(old, new, note):
            out.review.append(
                Finding(
                    f"{note}_changed",
                    "Ⅶ",
                    _(
                        "A pay raise, bonus or retirement allowance changes "
                        "(not required if only the timing changes)."
                    ),
                )
            )


# --- Ⅷ 退職 / Ⅸ その他 -----------------------------------------------------------


def _resignation(old: Snapshot, new: Snapshot, out: ChangeFindings) -> None:
    if _changed(old, new, "resignation_notice_days"):
        out.required.append(
            Finding("resignation_changed", "Ⅷ", _("Matters concerning resignation change."))
        )


def _insurance(old: Snapshot, new: Snapshot, out: ChangeFindings) -> None:
    before, after = set(old.get("insurances", [])), set(new.get("insurances", []))
    if any((name in before) != (name in after) for name in SOCIAL_INSURANCES):
        out.required.append(
            Finding(
                "social_insurance_changed",
                "Ⅸ",
                _("Health insurance or employees' pension coverage changes."),
            )
        )
    if any(name in after and name not in before for name in LABOR_INSURANCES):
        out.required.append(
            Finding("labor_insurance_applied", "Ⅸ", _("Labor insurance becomes applicable."))
        )
    if any(name in before and name not in after for name in LABOR_INSURANCES):
        out.review.append(
            Finding("labor_insurance_removed", "Ⅸ", _("Labor insurance is no longer applicable."))
        )
    if _changed(old, new, "periodic_health_check_interval"):
        out.review.append(
            Finding(
                "health_check_interval_changed",
                "Ⅸ",
                _("The periodic health check interval changes (required if longer than one year)."),
            )
        )


Rule = Callable[[Snapshot, Snapshot, ChangeFindings], None]

RULES: tuple[Rule, ...] = (
    _period,
    _renewal,
    _workplace,
    _duties,
    _working_hours,
    _holidays,
    _leave,
    _base_wage,
    _allowances,
    _premiums,
    _payment,
    _deductions,
    _benefits,
    _resignation,
    _insurance,
)
