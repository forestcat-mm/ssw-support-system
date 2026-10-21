"""一覧の画面の期限のタブ（仕様 5.1: すべて・期限超過・7日以内・14日以内〔・受理待ち〕。U2）。"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from django.http import QueryDict
from django.utils.translation import gettext_lazy as _

from services import deadlines

LEVEL_TAB_LABELS = {
    deadlines.Level.OVERDUE: _("Overdue"),
    deadlines.Level.WITHIN_7: _("Within 7 days"),
    deadlines.Level.WITHIN_14: _("Within 14 days"),
}
TAB_LEVELS = tuple(LEVEL_TAB_LABELS)


@dataclass(frozen=True)
class ListTab:
    label: Any
    count: int
    href: str
    selected: bool
    key: str  # data-level-count の値（"all"・"overdue" など）
    danger: bool = False  # 件数の札を danger にする（期限超過）


def query_without(params: QueryDict, *names: str) -> QueryDict:
    """今の条件から、タブで決める条件（level・status・page）を除いた写し。"""
    base = params.copy()
    for name in (*names, "page"):
        base.pop(name, None)
    return base


def level_tabs(
    base: QueryDict,
    total: int,
    counts: Mapping[deadlines.Level, int],
    selected: deadlines.Level | None,
    all_selected: bool | None = None,
) -> list[ListTab]:
    """「すべて」と期限のレベルのタブ。base は level を除いた今の条件。"""
    query = base.urlencode()
    tabs = [
        ListTab(
            label=_("All"),
            count=total,
            href=f"?{query}" if query else "?",
            selected=selected is None if all_selected is None else all_selected,
            key="all",
        )
    ]
    for level in TAB_LEVELS:
        params = base.copy()
        params["level"] = level.value
        tabs.append(
            ListTab(
                label=LEVEL_TAB_LABELS[level],
                count=counts.get(level, 0),
                href=f"?{params.urlencode()}",
                selected=selected is level,
                key=level.value,
                danger=level is deadlines.Level.OVERDUE and counts.get(level, 0) > 0,
            )
        )
    return tabs
