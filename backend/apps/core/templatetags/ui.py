"""業務画面の表示部品（警告レベルのバッジ・分野のタグ・外国人の状態）。"""

from datetime import date
from typing import Any

from django import template
from django.utils import timezone
from django.utils.translation import gettext as _

from services import deadlines

register = template.Library()

LEVEL_CLASSES = {
    deadlines.Level.OVERDUE: "badge--overdue",
    deadlines.Level.WITHIN_7: "badge--within-7",
    deadlines.Level.WITHIN_14: "badge--within-14",
    deadlines.Level.OPEN: "badge--open",
    deadlines.Level.NOT_YET: "badge--not-yet",
}


def level_text(assessment: deadlines.Assessment, due_on: date) -> str:
    """色だけに頼らない文字（「超過 5日」「今日まで」「あと3日」、受付前は日付）。"""
    if assessment.level is deadlines.Level.NOT_YET:
        return due_on.isoformat()
    if assessment.days_left < 0:
        return _("Overdue %(days)s d") % {"days": -assessment.days_left}
    if assessment.days_left == 0:
        return _("Due today")
    return _("%(days)s d left") % {"days": assessment.days_left}


@register.inclusion_tag("ui/components/level_badge.html")
def level_badge(deadline: deadlines.Deadline, today: date | None = None) -> dict[str, Any]:
    """T10 の Deadline から警告レベルのバッジ。

    色は繰り延べ前の期限が基準。閉庁日で繰り延べた法令上の期限は title に出す。
    """
    today = today or timezone.localdate()
    assessment = deadlines.assess(deadline, today)
    return {
        "css": LEVEL_CLASSES[assessment.level],
        "text": level_text(assessment, deadline.unadjusted_due_on),
        "due_on": deadline.unadjusted_due_on,
        "legal_due_on": deadline.due_on if deadline.adjusted else None,
        "level": assessment.level.value,
    }


@register.inclusion_tag("ui/components/level_count_badge.html")
def level_count_badge(level: deadlines.Level, count: int) -> dict[str, Any]:
    """一覧の見出しに並べる警告レベルの人数（押すとそのレベルで絞り込む）。"""
    texts = {
        deadlines.Level.OVERDUE: _("Overdue: %(count)s"),
        deadlines.Level.WITHIN_7: _("Within 7 days: %(count)s"),
        deadlines.Level.WITHIN_14: _("Within 14 days: %(count)s"),
    }
    return {
        "css": LEVEL_CLASSES[level],
        "level": level.value,
        "text": texts[level] % {"count": count},
    }


FIELD_CLASSES = {"industrial_products": "tag--industrial", "food_manufacturing": "tag--food"}


@register.inclusion_tag("ui/components/field_tag.html")
def field_tag(field: Any) -> dict[str, Any]:
    return {"field": field, "css": FIELD_CLASSES.get(getattr(field, "code", ""), "")}


STATUS_CLASSES = {
    "pre_entry": "status--pre-entry",
    "residing": "",
    "left_job": "status--left",
    "returned": "status--returned",
}


@register.inclusion_tag("ui/components/worker_status.html")
def worker_status(status: str, label: str, deactivated: bool = False) -> dict[str, Any]:
    css = "status--deactivated" if deactivated else STATUS_CLASSES.get(status, "")
    return {"css": css, "label": label}
