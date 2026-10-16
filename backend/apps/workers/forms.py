"""外国人の業務画面のフォーム（入力の検査だけ。検索・判定は services に置く）。"""

from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.core.models import Staff, User
from apps.hosts.models import HostOrganization, SpecifiedSkillField
from apps.workers.models import WorkerStatus
from services import deadlines
from services.worker_list import DEFAULT_STATUSES, Order, WorkerListQuery

STATUS_DEFAULT = ""
STATUS_ALL = "all"
STAFF_MINE = "mine"
STAFF_NONE = "none"

LEVEL_CHOICES = [
    (deadlines.Level.OVERDUE.value, _("overdue")),
    (deadlines.Level.WITHIN_7.value, _("within 7 days")),
    (deadlines.Level.WITHIN_14.value, _("within 14 days")),
    (deadlines.Level.OPEN.value, _("open")),
    (deadlines.Level.NOT_YET.value, _("not yet open")),
]


class WorkerListForm(forms.Form):
    """外国人一覧の検索・絞り込み（GET。条件は URL に残す）。"""

    q = forms.CharField(label=_("Search"), required=False, max_length=100)
    host = forms.ModelChoiceField(
        label=_("host organization"),
        queryset=HostOrganization.objects.order_by("name_kana", "name"),
        required=False,
        empty_label=_("All host organizations"),
    )
    staff = forms.ChoiceField(label=_("assigned staff"), required=False)
    status = forms.ChoiceField(
        label=_("status"),
        required=False,
        choices=[
            (STATUS_DEFAULT, _("Before entry and residing")),
            (STATUS_ALL, _("All statuses")),
            *WorkerStatus.choices,
        ],
    )
    field = forms.ModelChoiceField(
        label=_("specified skill field"),
        queryset=SpecifiedSkillField.objects.filter(is_active=True).order_by("sort_order"),
        required=False,
        empty_label=_("All fields"),
    )
    level = forms.ChoiceField(
        label=_("deadline level"),
        required=False,
        choices=[("", _("All levels")), *LEVEL_CHOICES],
    )
    order = forms.ChoiceField(
        label=_("order"),
        required=False,
        choices=[
            (Order.EXPIRY, _("Expiry date (nearest first)")),
            (Order.NAME, _("Name (romaji)")),
        ],
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        staff_members = Staff.objects.filter(is_active=True).order_by("name_kana", "name")
        staff_field = self.fields["staff"]
        assert isinstance(staff_field, forms.ChoiceField)
        staff_field.choices = [
            ("", _("All staff")),
            (STAFF_MINE, _("Assigned to me")),
            (STAFF_NONE, _("Not assigned")),
            *[(str(member.pk), member.name) for member in staff_members],
        ]
        for name, bound_field in self.fields.items():
            if name != "q":
                bound_field.widget.attrs["class"] = "select"

    def to_query(self, user: User) -> WorkerListQuery:
        """検査を通った条件だけを使う（不正な値は既定に戻す）。"""
        self.is_valid()
        data = self.cleaned_data
        staff = data.get("staff") or ""
        assigned_staff_id: int | None = None
        if staff == STAFF_MINE:
            own = Staff.objects.filter(user=user).values_list("pk", flat=True).first()
            assigned_staff_id = own if own is not None else 0  # 担当者として未登録なら 0 件
        elif staff.isdigit():
            assigned_staff_id = int(staff)
        status = data.get("status") or STATUS_DEFAULT
        statuses = (
            DEFAULT_STATUSES
            if status == STATUS_DEFAULT
            else ()
            if status == STATUS_ALL
            else (status,)
        )
        host = data.get("host")
        skill_field = data.get("field")
        level = data.get("level")
        return WorkerListQuery(
            text=data.get("q") or "",
            host_organization_id=host.pk if host else None,
            assigned_staff_id=assigned_staff_id,
            unassigned_only=staff == STAFF_NONE,
            statuses=statuses,
            field_id=skill_field.pk if skill_field else None,
            level=deadlines.Level(level) if level else None,
            order=data.get("order") or Order.EXPIRY,
        )
