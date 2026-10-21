"""業務画面の表示部品（警告レベルのバッジ・分野のタグ・外国人の状態）。"""

from datetime import date, timedelta
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
def level_badge(
    deadline: deadlines.Deadline, today: date | None = None, not_yet_label: bool = False
) -> dict[str, Any]:
    """T10 の Deadline から警告レベルのバッジ。

    色は繰り延べ前の期限が基準。閉庁日で繰り延べた法令上の期限は title に出す。受付前は日付を
    出すが、期限の日付を別の列に出す表（届出タスクの表）では not_yet_label で「受付前」と出す。
    """
    today = today or timezone.localdate()
    assessment = deadlines.assess(deadline, today)
    text = level_text(assessment, deadline.unadjusted_due_on)
    if not_yet_label and assessment.level is deadlines.Level.NOT_YET:
        text = _("not yet open")
    return {
        "css": LEVEL_CLASSES[assessment.level],
        "text": text,
        "due_on": deadline.unadjusted_due_on,
        "legal_due_on": deadline.due_on if deadline.adjusted else None,
        "level": assessment.level.value,
    }


@register.inclusion_tag("ui/components/due_date.html")
def due_date(
    deadline: deadlines.Deadline | None, today: date | None = None, badge: bool = True
) -> dict[str, Any]:
    """期限の日付（等幅）と警告バッジ。受付前はバッジが日付そのものなので日付を重ねて出さない。"""
    if deadline is None:
        return {"deadline": None}
    today = today or timezone.localdate()
    level = deadlines.assess(deadline, today).level
    return {
        "deadline": deadline,
        "today": today,
        "badge": badge,
        "date_only": not badge or level is deadlines.Level.NOT_YET,
        "show_date": not badge or level is not deadlines.Level.NOT_YET,
    }


@register.filter
def add_days(value: date | None, days: int) -> date | None:
    """日付に日数を足す（例: 無期転換の日＝契約の終了日の翌日）。"""
    return None if value is None else value + timedelta(days=int(days))


@register.filter
def yen(value: int | None) -> str:
    """金額の 3 桁区切り（¥ は付けない）。"""
    return "" if value is None else f"{value:,}"


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


@register.inclusion_tag("ui/components/form_field.html")
def form_field(bound: Any, conditional: bool = False) -> dict[str, Any]:
    """入力欄 1 つ（ラベル・必須の印・入力欄・補足・項目のエラー）。

    conditional は任意の区画の欄（区画に入れる場合だけ必須。印は「入れる場合は必須」。B5）。
    """
    if bound.errors:
        described = f"{bound.auto_id}-error"
        bound.field.widget.attrs["aria-invalid"] = "true"
        bound.field.widget.attrs["aria-describedby"] = described
    elif bound.help_text:
        bound.field.widget.attrs["aria-describedby"] = f"{bound.auto_id}-help"
    return {"field": bound, "conditional": conditional}


@register.simple_tag
def category_label(category: Any, today: date) -> str:
    """業務区分の名称。適用期間外なら印を付ける（飲食料品製造業は 2027-04-01 に切り替わる）。"""
    in_effect = (category.valid_from is None or category.valid_from <= today) and (
        category.valid_until is None or category.valid_until >= today
    )
    if in_effect:
        return str(category.name)
    note = _("not in effect")
    return f"{category.name}（{note}）"


@register.filter
def model_label(content_type: Any) -> str:
    """監査ログの対象の種類（モデルの表示名）。"""
    if content_type is None:
        return "—"
    model = content_type.model_class()
    return str(model._meta.verbose_name) if model is not None else str(content_type.model)


@register.filter
def active_attachments(attachments: Any) -> list[Any]:
    """取り消していない添付だけ（並び順はそのまま。先頭が今の添付）。"""
    return [attachment for attachment in attachments if attachment.voided_at is None]


@register.filter
def get_item(mapping: Any, key: Any) -> Any:
    """辞書の値（無ければ None）。行の ID ごとの結果を表の行で引く（T42）。"""
    return mapping.get(key) if hasattr(mapping, "get") else None


@register.filter
def bound_field(form: Any, name: str) -> Any:
    """フォームの欄（名前で引く。ひな形の印の欄を先頭にまとめる。T41）。"""
    return form[name]


@register.filter
def sections_without(form: Any, names: Any) -> Any:
    """版のフォームの節（names の欄を除く。T41）。"""
    return form.sections(exclude=set(names or ()))


@register.filter
def fields_except(form: Any, names: str) -> list[Any]:
    """フォームの欄（names〔空白区切り〕の欄を除く）。一覧の絞り込みで、タブで決める欄を除く（U2）。"""
    skip = set(names.split())
    return [bound for bound in form if bound.name not in skip]


@register.filter
def minutes_label(value: int | None) -> str:
    """分を「8時間0分」の形に（契約のタブの所定労働時間。U5）。"""
    if value is None:
        return "—"
    hours, minutes = divmod(int(value), 60)
    return _("%(hours)s h %(minutes)s min") % {"hours": hours, "minutes": minutes}
