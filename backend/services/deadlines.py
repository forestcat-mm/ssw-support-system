"""期限計算（第5章 F06・F07）。DB に依存しない純粋関数だけを置く。

T11（届出タスクの自動起票）と T14（期限ダッシュボード）の両方がここを呼ぶ。
種別ごとの関数が日付だけの Deadline を返し、今日に対する警告レベルは assess() で求める。

期限の数え方（2026-10 時点で確認した根拠）:
- 日で定める期間は初日を算入しない（民法第140条）。末日の終了で満了（第141条）。
  → 随時届出「事由が生じた日から14日以内」は、事由の日＋14日。
- 国の行政庁への届出の期限が行政機関の休日に当たるときは、翌日を期限とみなす
  （行政機関の休日に関する法律 第2条）。休日は土日・祝日・12/29〜1/3（同第1条）。
  → 随時届出だけ繰り延べる。色分け（T14）は安全側として繰り延べ前の日で行う。
- 月で定める期間は暦に従い、応当日の前日に満了、応当日が無ければその月の末日
  （民法第143条）。→ 定期面談「前回から3か月以内」。
- 登録支援機関の登録の更新は、有効期間満了日の6か月前の月の初日から4か月前の月の末日まで。
  3か月前の月の末日を過ぎると有効期間内の更新が認められない（出入国在留管理庁の案内）。
"""

from __future__ import annotations

import calendar
import enum
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol

# --- 種別と警告レベル ---------------------------------------------------------------


class DeadlineKind(enum.StrEnum):
    RESIDENCE_EXPIRY = "residence_expiry"  # 在留期限（更新申請は3か月前から）
    AD_HOC_FILING = "ad_hoc_filing"  # 随時届出（事由から14日以内）
    PERIODIC_FILING = "periodic_filing"  # 定期届出（4/1〜5/31）
    PERIODIC_INTERVIEW = "periodic_interview"  # 定期面談（前回から3か月以内）
    REGISTRATION_RENEWAL = "registration_renewal"  # 自機関の登録更新
    DOCUMENT_EXPIRY = "document_expiry"  # 加入証明・添付の有効期限
    PASSPORT_EXPIRY = "passport_expiry"  # 旅券の有効期限
    SUPPORT_CONTRACT_END = "support_contract_end"  # 支援委託契約の終了日
    EMPLOYMENT_CONTRACT_EXPIRY = "employment_contract_expiry"  # 雇用契約の期間満了（終了未登録）


class Level(enum.StrEnum):
    NOT_YET = "not_yet"  # 受付・着手の日より前
    OPEN = "open"  # 対応期間中（期限まで15日以上）
    WITHIN_14 = "within_14"  # 期限まで8〜14日
    WITHIN_7 = "within_7"  # 期限まで0〜7日（当日を含む）
    OVERDUE = "overdue"  # 期限の翌日以降


# 並び順（1 = 最重要）。在留期限の超過はオーバーステイにつながるため最重要。
PRIORITY: dict[DeadlineKind, int] = {
    DeadlineKind.RESIDENCE_EXPIRY: 1,
    DeadlineKind.AD_HOC_FILING: 2,
    DeadlineKind.PERIODIC_FILING: 2,
    DeadlineKind.REGISTRATION_RENEWAL: 2,
    DeadlineKind.PERIODIC_INTERVIEW: 3,
    DeadlineKind.EMPLOYMENT_CONTRACT_EXPIRY: 3,
    DeadlineKind.PASSPORT_EXPIRY: 3,
    DeadlineKind.SUPPORT_CONTRACT_END: 3,
    DeadlineKind.DOCUMENT_EXPIRY: 4,
}

# 仕様書に定めの無い「受付・着手の日」の前倒し月数（業務ルール。2026-10 に決定）
LEAD_MONTHS: dict[DeadlineKind, int] = {
    DeadlineKind.RESIDENCE_EXPIRY: 3,  # 更新申請の受付開始（入管の運用）
    DeadlineKind.DOCUMENT_EXPIRY: 1,
    DeadlineKind.PASSPORT_EXPIRY: 6,
    DeadlineKind.SUPPORT_CONTRACT_END: 3,
    DeadlineKind.EMPLOYMENT_CONTRACT_EXPIRY: 1,
}

AD_HOC_FILING_DAYS = 14
INTERVIEW_INTERVAL_MONTHS = 3
WITHIN_7_DAYS = 7
WITHIN_14_DAYS = 14


@dataclass(frozen=True)
class Deadline:
    """期限（日付だけ。今日に依存しないので T11 は届出タスクに保存できる）。

    due_on は法令上の期限（閉庁日の繰り延べ後）。unadjusted_due_on は繰り延べ前の日で、
    T14 の色分けはこちらを基準にする（安全側）。
    """

    kind: DeadlineKind
    basis_on: date  # 元の日付（事由の日、在留期限、前回の面談日など）
    due_on: date
    unadjusted_due_on: date
    opens_on: date | None  # 受付・着手の日（T11 の起票日）
    final_on: date | None = None  # 最終の限度（登録更新の「3か月前の月末」）

    @property
    def priority(self) -> int:
        return PRIORITY[self.kind]

    @property
    def adjusted(self) -> bool:
        return self.due_on != self.unadjusted_due_on


@dataclass(frozen=True)
class Assessment:
    level: Level
    days_left: int  # 繰り延べ前の期限日 − 今日（負の値は超過日数）
    legal_days_left: int  # 法令上の期限日 − 今日


def assess(deadline: Deadline, today: date) -> Assessment:
    """今日に対する警告レベル。色分けは繰り延べ前の期限日を基準にする。"""
    days_left = (deadline.unadjusted_due_on - today).days
    legal_days_left = (deadline.due_on - today).days
    if days_left < 0:
        level = Level.OVERDUE
    elif days_left <= WITHIN_7_DAYS:
        level = Level.WITHIN_7
    elif days_left <= WITHIN_14_DAYS:
        level = Level.WITHIN_14
    elif deadline.opens_on is not None and today < deadline.opens_on:
        level = Level.NOT_YET
    else:
        level = Level.OPEN
    return Assessment(level=level, days_left=days_left, legal_days_left=legal_days_left)


# --- 暦の計算 --------------------------------------------------------------------


def end_of_month(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _shift_month(year: int, month: int, months: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + months
    return index // 12, index % 12 + 1


def add_months(day: date, months: int) -> date:
    """同じ日付で n か月ずらす（負の値は前）。その日が無い月は月末。

    例: 11/30 の3か月前 → 8/30、5/31 の3か月前 → 2/28（うるう年は 2/29）。
    """
    year, month = _shift_month(day.year, day.month, months)
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def period_start(event_on: date) -> date:
    """期間の起算日（民法第140条: 初日不算入。事由の日の翌日から数える）。ADR-0008。"""
    return event_on + timedelta(days=1)


def days_period_end(event_on: date, days: int) -> date:
    """事由の日から n 日の期間の末日（民法第140条・第141条）。

    翌日から数えて n 日目の終了で満了。例: 9/16 から 14 日 → 9/17 起算 → 9/30。
    """
    return period_start(event_on) + timedelta(days=days - 1)


def months_period_end(event_on: date, months: int) -> date:
    """事由の日から n か月の期間の末日（民法第140条・第143条）。ADR-0008。

    初日不算入で翌日から起算し、応当日の前日に満了。応当日が無ければその月の末日。
    例: 1/15 → 4/15、11/30 → 2/28（2/29）、2/28（平年）→ 5/31、1/31 → 4/30。
    """
    start = period_start(event_on)
    year, month = _shift_month(start.year, start.month, months)
    if start.day > calendar.monthrange(year, month)[1]:
        return end_of_month(year, month)
    return date(year, month, start.day) - timedelta(days=1)


class HolidayCalendar(Protocol):
    def is_closed(self, day: date) -> bool: ...


class GovernmentHolidayCalendar:
    """行政機関の休日（行政機関の休日に関する法律 第1条）: 土日・祝日・12/29〜1/3。

    祝日は jpholiday で判定する（外部 API に接続しない）。年 1 回 jpholiday を更新し、
    翌年の祝日（法改正・特例を含む）を確認する（T15 の運用手順）。
    """

    def is_closed(self, day: date) -> bool:
        import jpholiday

        return (
            day.weekday() >= 5
            or (day.month == 12 and day.day >= 29)
            or (day.month == 1 and day.day <= 3)
            or bool(jpholiday.is_holiday(day))
        )


GOVERNMENT_CALENDAR = GovernmentHolidayCalendar()


def next_open_day(day: date, holidays: HolidayCalendar) -> date:
    """閉庁日なら次の開庁日まで繰り延べる。"""
    while holidays.is_closed(day):
        day += timedelta(days=1)
    return day


def _fixed(kind: DeadlineKind, due_on: date, opens_on: date | None) -> Deadline:
    return Deadline(
        kind=kind, basis_on=due_on, due_on=due_on, unadjusted_due_on=due_on, opens_on=opens_on
    )


def _lead(kind: DeadlineKind, due_on: date) -> date:
    return add_months(due_on, -LEAD_MONTHS[kind])


# --- 種別ごとの期限 ---------------------------------------------------------------


def residence_expiry(expires_on: date) -> Deadline:
    """在留期限。期限の当日が期限（閉庁日でも繰り延べない）。更新申請は3か月前から。"""
    kind = DeadlineKind.RESIDENCE_EXPIRY
    return _fixed(kind, expires_on, _lead(kind, expires_on))


def ad_hoc_filing(event_on: date, holidays: HolidayCalendar = GOVERNMENT_CALENDAR) -> Deadline:
    """随時届出。事由の日（契約の終了日・版の変更日など）から 14 日以内（初日不算入。ADR-0008）。

    閉庁日なら次の開庁日（行政機関の休日に関する法律 第2条）。
    """
    unadjusted = days_period_end(event_on, AD_HOC_FILING_DAYS)
    return Deadline(
        kind=DeadlineKind.AD_HOC_FILING,
        basis_on=event_on,
        due_on=next_open_day(unadjusted, holidays),
        unadjusted_due_on=unadjusted,
        opens_on=event_on,
    )


def periodic_filing(year: int) -> Deadline:
    """定期届出（その年の 4/1〜5/31。5/31 が閉庁日でも繰り延べない）。"""
    due = date(year, 5, 31)
    return Deadline(
        kind=DeadlineKind.PERIODIC_FILING,
        basis_on=date(year, 4, 1),
        due_on=due,
        unadjusted_due_on=due,
        opens_on=date(year, 4, 1),
    )


def next_periodic_filing(today: date) -> Deadline:
    """基準日に対応する定期届出（5/31 までは今年、6/1 以降は翌年）。"""
    return periodic_filing(today.year if today <= date(today.year, 5, 31) else today.year + 1)


def periodic_interview(last_interview_on: date, scheduled_on: date | None = None) -> Deadline:
    """定期面談（前回から3か月以内。民法第143条）。

    面談記録がまだ無い場合、呼び出し側は支援の開始日（雇用契約の開始日）を渡す。
    次回の予定日（scheduled_on）があれば期限はその日にし、法定の期限は final_on に残す。
    予定日が法定の期限を超えるときは ValueError（入力の検査は呼び出し側で行う）。
    """
    legal = months_period_end(last_interview_on, INTERVIEW_INTERVAL_MONTHS)
    if scheduled_on is not None and not last_interview_on < scheduled_on <= legal:
        raise ValueError("scheduled_on must be after the last interview and within 3 months")
    due = scheduled_on or legal
    return Deadline(
        kind=DeadlineKind.PERIODIC_INTERVIEW,
        basis_on=last_interview_on,
        due_on=due,
        unadjusted_due_on=due,
        opens_on=last_interview_on + timedelta(days=1),
        final_on=legal if scheduled_on is not None else None,
    )


def registration_renewal(expires_on: date) -> Deadline:
    """自機関の登録更新。受付は満了日の6か月前の月の初日から、期限は4か月前の月の末日。

    final_on（3か月前の月の末日）を過ぎると、有効期間内の更新が認められない。
    """
    open_year, open_month = _shift_month(expires_on.year, expires_on.month, -6)
    due_year, due_month = _shift_month(expires_on.year, expires_on.month, -4)
    final_year, final_month = _shift_month(expires_on.year, expires_on.month, -3)
    due = end_of_month(due_year, due_month)
    return Deadline(
        kind=DeadlineKind.REGISTRATION_RENEWAL,
        basis_on=expires_on,
        due_on=due,
        unadjusted_due_on=due,
        opens_on=date(open_year, open_month, 1),
        final_on=end_of_month(final_year, final_month),
    )


def document_expiry(valid_until: date) -> Deadline:
    """加入証明（構成員証明書など）・添付の有効期限。"""
    kind = DeadlineKind.DOCUMENT_EXPIRY
    return _fixed(kind, valid_until, _lead(kind, valid_until))


def passport_expiry(expires_on: date) -> Deadline:
    kind = DeadlineKind.PASSPORT_EXPIRY
    return _fixed(kind, expires_on, _lead(kind, expires_on))


def support_contract_end(ends_on: date) -> Deadline:
    kind = DeadlineKind.SUPPORT_CONTRACT_END
    return _fixed(kind, ends_on, _lead(kind, ends_on))


def employment_contract_expiry(ends_on: date) -> Deadline:
    """雇用契約の期間満了。終了が未登録のまま期限を過ぎたら超過（終了の登録を促す）。"""
    kind = DeadlineKind.EMPLOYMENT_CONTRACT_EXPIRY
    return _fixed(kind, ends_on, _lead(kind, ends_on))
