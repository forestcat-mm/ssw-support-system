"""期限計算（services/deadlines.py）。DB を使わない固定日付のテスト。"""

from collections.abc import Callable
from datetime import date, timedelta

import pytest

from services import deadlines as d
from services.deadlines import DeadlineKind, Level

# --- 暦の計算 --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "months", "expected"),
    [
        (date(2026, 11, 30), -3, date(2026, 8, 30)),  # 11/30 の3か月前
        (date(2026, 5, 31), -3, date(2026, 2, 28)),  # 応当日が無い → 月末
        (date(2028, 5, 31), -3, date(2028, 2, 29)),  # うるう年
        (date(2026, 8, 31), -3, date(2026, 5, 31)),
        (date(2027, 1, 15), -3, date(2026, 10, 15)),  # 年をまたぐ
        (date(2026, 1, 31), 1, date(2026, 2, 28)),
    ],
)
def test_add_months_clamps_to_month_end(day: date, months: int, expected: date) -> None:
    assert d.add_months(day, months) == expected


@pytest.mark.parametrize(
    ("event_on", "expected"),
    [
        (date(2026, 1, 15), date(2026, 4, 15)),
        (date(2026, 1, 31), date(2026, 4, 30)),
        (date(2026, 11, 30), date(2027, 2, 28)),
        (date(2027, 11, 30), date(2028, 2, 29)),  # うるう年
        (date(2027, 2, 28), date(2027, 5, 31)),  # 平年の 2/28 → 3/1 起算 → 5/31
        (date(2028, 2, 29), date(2028, 5, 31)),  # うるう年の 2/29 → 3/1 起算 → 5/31
        (date(2028, 2, 28), date(2028, 5, 28)),  # うるう年の 2/28 → 2/29 起算 → 5/28
        (date(2026, 10, 30), date(2027, 1, 30)),
    ],
)
def test_months_period_end_follows_civil_code(event_on: date, expected: date) -> None:
    assert d.months_period_end(event_on, 3) == expected


@pytest.mark.parametrize(
    ("day", "closed"),
    [
        (date(2026, 10, 16), False),  # 金曜
        (date(2026, 10, 17), True),  # 土曜
        (date(2026, 10, 18), True),  # 日曜
        (date(2026, 10, 12), True),  # スポーツの日
        (date(2026, 5, 6), True),  # 振替休日
        (date(2026, 9, 22), True),  # 国民の休日
        (date(2026, 12, 29), True),  # 年末年始（平日）
        (date(2027, 1, 4), False),
    ],
)
def test_government_holidays(day: date, closed: bool) -> None:
    assert d.GOVERNMENT_CALENDAR.is_closed(day) is closed


# --- 随時届出 --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event_on", "unadjusted", "due"),
    [
        (date(2026, 9, 16), date(2026, 9, 30), date(2026, 9, 30)),  # 平日のまま
        (date(2026, 10, 3), date(2026, 10, 17), date(2026, 10, 19)),  # 土曜 → 月曜
        (date(2026, 9, 28), date(2026, 10, 12), date(2026, 10, 13)),  # 祝日の月曜 → 火曜
        (date(2026, 9, 7), date(2026, 9, 21), date(2026, 9, 24)),  # 連休（敬老の日〜秋分の日）
        (date(2026, 4, 22), date(2026, 5, 6), date(2026, 5, 7)),  # 振替休日
        (date(2026, 12, 17), date(2026, 12, 31), date(2027, 1, 4)),  # 年末年始
        (date(2026, 12, 28), date(2027, 1, 11), date(2027, 1, 12)),  # 成人の日
        (date(2026, 12, 26), date(2027, 1, 9), date(2027, 1, 12)),  # 土日 + 成人の日
    ],
)
def test_ad_hoc_filing_is_event_plus_14_days_rolled_to_open_day(
    event_on: date, unadjusted: date, due: date
) -> None:
    deadline = d.ad_hoc_filing(event_on)
    assert deadline.kind is DeadlineKind.AD_HOC_FILING
    assert deadline.unadjusted_due_on == unadjusted
    assert deadline.due_on == due
    assert deadline.adjusted is (due != unadjusted)
    assert deadline.opens_on == event_on


def test_ad_hoc_filing_accepts_a_custom_calendar() -> None:
    class NoHolidays:
        def is_closed(self, day: date) -> bool:
            return False

    assert d.ad_hoc_filing(date(2026, 10, 3), NoHolidays()).due_on == date(2026, 10, 17)


# --- 在留期限・定期届出・定期面談・登録更新 ------------------------------------------


def test_residence_expiry_opens_three_months_before_and_is_top_priority() -> None:
    deadline = d.residence_expiry(date(2026, 11, 30))
    assert deadline.opens_on == date(2026, 8, 30)
    assert deadline.due_on == date(2026, 11, 30)  # 閉庁日でも繰り延べない
    assert deadline.priority == 1
    saturday = d.residence_expiry(date(2026, 10, 17))
    assert saturday.due_on == date(2026, 10, 17)


@pytest.mark.parametrize(
    ("today", "year"),
    [(date(2026, 3, 31), 2026), (date(2026, 5, 31), 2026), (date(2026, 6, 1), 2027)],
)
def test_next_periodic_filing(today: date, year: int) -> None:
    deadline = d.next_periodic_filing(today)
    assert (deadline.opens_on, deadline.due_on) == (date(year, 4, 1), date(year, 5, 31))


def test_periodic_filing_is_not_rolled_on_weekend() -> None:
    assert d.periodic_filing(2026).due_on == date(2026, 5, 31)  # 日曜でもそのまま


def test_periodic_interview() -> None:
    deadline = d.periodic_interview(date(2026, 11, 30))
    assert deadline.due_on == date(2027, 2, 28)
    assert deadline.opens_on == date(2026, 12, 1)


@pytest.mark.parametrize(
    ("expires_on", "opens_on", "due_on", "final_on"),
    [
        # 出入国在留管理庁の案内の例
        (date(2024, 5, 1), date(2023, 11, 1), date(2024, 1, 31), date(2024, 2, 29)),
        (date(2024, 6, 30), date(2023, 12, 1), date(2024, 2, 29), date(2024, 3, 31)),
        (date(2029, 3, 31), date(2028, 9, 1), date(2028, 11, 30), date(2028, 12, 31)),
    ],
)
def test_registration_renewal(
    expires_on: date, opens_on: date, due_on: date, final_on: date
) -> None:
    deadline = d.registration_renewal(expires_on)
    assert (deadline.opens_on, deadline.due_on, deadline.final_on) == (opens_on, due_on, final_on)


@pytest.mark.parametrize(
    ("factory", "months"),
    [
        (d.document_expiry, 1),
        (d.passport_expiry, 6),
        (d.support_contract_end, 3),
        (d.employment_contract_expiry, 1),
    ],
)
def test_lead_months_of_fixed_date_deadlines(
    factory: Callable[[date], d.Deadline], months: int
) -> None:
    deadline = factory(date(2027, 8, 31))
    assert deadline.due_on == date(2027, 8, 31)
    assert deadline.opens_on == d.add_months(date(2027, 8, 31), -months)


# --- 警告レベル --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("days_left", "level"),
    [
        (100, Level.NOT_YET),  # 受付・着手の日より前
        (30, Level.OPEN),
        (15, Level.OPEN),
        (14, Level.WITHIN_14),
        (8, Level.WITHIN_14),
        (7, Level.WITHIN_7),
        (0, Level.WITHIN_7),  # 期限の当日
        (-1, Level.OVERDUE),
    ],
)
def test_assess_levels(days_left: int, level: Level) -> None:
    deadline = d.residence_expiry(date(2026, 11, 30))  # 受付開始は 8/30（期限の92日前）
    today = deadline.due_on - timedelta(days=days_left)
    result = d.assess(deadline, today)
    assert result.level is level
    assert result.days_left == days_left


def test_assess_uses_unadjusted_date_for_level_but_reports_legal_days() -> None:
    deadline = d.ad_hoc_filing(date(2026, 10, 3))  # 繰り延べ前 10/17（土）、法令上 10/19（月）
    result = d.assess(deadline, date(2026, 10, 18))
    assert result.level is Level.OVERDUE  # 色分けは繰り延べ前を基準にする
    assert result.days_left == -1
    assert result.legal_days_left == 1  # 法令上はまだ 1 日ある


def test_priorities_cover_every_kind() -> None:
    assert set(d.PRIORITY) == set(DeadlineKind)
