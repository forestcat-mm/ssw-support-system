"""監査ログから「いつから誰が」の履歴の行を組み立てる（T45-D3。ADR-0037）。

マイグレーション（外国人の担当者・事務所の支援責任者）から使うので、モデルを import しない。
値は監査ログのとおり担当者の主キー（無ければ None）。
"""

import datetime
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class Change:
    on: datetime.date  # 変更の日（東京の日付）
    old: int | None
    new: int | None


@dataclass(frozen=True)
class Period:
    staff_id: int | None
    starts_on: datetime.date
    is_estimated: bool


def rebuild(
    *,
    registered_on: datetime.date,
    created_with: tuple[int | None] | None,
    changes: Iterable[Change],
    current: int | None,
    current_since: datetime.date,
) -> list[Period]:
    """登録の日・作成の記録（(値,)。記録が無ければ None）・変更の記録（古い順）・今の値から
    行を作る。

    - 最初の行の開始日は登録の日。作成の記録が無ければ、最初の値は推定（最初の変更の前の値か、
      変更が無ければ今の値）。
    - 同じ日の変更は、その日の最後の値だけを残す。前の行と同じ担当者になる変更は行にしない。
    - 最後の値が今の値と違えば（監査ログの外の変更）、今の値の推定の行を current_since の日で足す。
    - 担当なし（None）の行は、担当者のいた行の後にだけ作る。
    """
    ordered = list(changes)
    if created_with is not None:
        first, estimated = created_with[0], False
    else:
        first = ordered[0].old if ordered else current
        estimated = True
    periods: list[Period] = []

    def add(staff_id: int | None, on: datetime.date, is_estimated: bool) -> None:
        if periods and periods[-1].starts_on == on:
            periods.pop()  # 同じ日はその日の最後の値
        if (periods and periods[-1].staff_id == staff_id) or (not periods and staff_id is None):
            return
        periods.append(Period(staff_id, on, is_estimated))

    add(first, registered_on, estimated)
    for change in ordered:
        add(change.new, max(change.on, registered_on), False)
    last = periods[-1].staff_id if periods else None
    if last != current:
        since = current_since
        if periods and since <= periods[-1].starts_on:
            since = periods[-1].starts_on + datetime.timedelta(days=1)
        add(current, since, True)
    return periods
