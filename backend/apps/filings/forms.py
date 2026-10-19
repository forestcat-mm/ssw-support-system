"""届出タスクの業務画面のフォーム（T14）。遷移のルールと起票は services.filings に任せる。"""

from typing import Any

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from apps.contracts.models import EmploymentContract
from apps.core.models import Staff, User
from apps.core.web_forms import StyledFormMixin
from apps.filings.models import Filing, FilingKind, FilingStatus
from apps.hosts.models import HostOrganization
from apps.workers.models import ForeignWorker
from services import dashboard, deadlines, filings, workers

ASSIGNEE_ALL = "all"
ASSIGNEE_MINE = "mine"
ASSIGNEE_NONE = "none"
STATUS_OPEN = ""
STATUS_AWAITING = "submitted"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"
STATUS_ALL = "all"

STATUS_GROUPS: dict[str, tuple[str, ...]] = {
    STATUS_OPEN: dashboard.OPEN_STATUSES,
    STATUS_AWAITING: dashboard.AWAITING_RECEIPT,
    STATUS_COMPLETED: dashboard.COMPLETED,
    STATUS_CANCELLED: (FilingStatus.CANCELLED,),
    STATUS_ALL: (),
}


def own_staff_id(user: User) -> int | None:
    return Staff.objects.filter(user=user).values_list("pk", flat=True).first()


def active_staff() -> Any:
    return Staff.objects.filter(is_active=True).order_by("name_kana", "name")


class DashboardForm(forms.Form):
    """ダッシュボードの絞り込み（GET。条件は URL に残す）。"""

    assignee = forms.ChoiceField(label=_("assignee"), required=False)
    level = forms.ChoiceField(
        label=_("deadline level"),
        required=False,
        choices=[
            ("", _("All levels")),
            (deadlines.Level.OVERDUE.value, _("overdue")),
            (deadlines.Level.WITHIN_7.value, _("within 7 days")),
            (deadlines.Level.WITHIN_14.value, _("within 14 days")),
            (deadlines.Level.OPEN.value, _("open")),
            (deadlines.Level.NOT_YET.value, _("not yet open")),
        ],
    )
    kind = forms.ChoiceField(
        label=_("kind"), required=False, choices=[("", _("All kinds")), *FilingKind.choices]
    )
    filer = forms.ChoiceField(
        label=_("filer"),
        required=False,
        choices=[
            ("", _("All filers")),
            *[(f.value, label) for f, label in filings.FILER_LABELS.items()],
        ],
    )
    status = forms.ChoiceField(
        label=_("status"),
        required=False,
        choices=[
            (STATUS_OPEN, _("Not started and in progress")),
            (STATUS_AWAITING, _("Awaiting receipt (submitted)")),
            (STATUS_COMPLETED, _("Completed (accepted or not required)")),
            (STATUS_CANCELLED, _("Cancelled")),
            (STATUS_ALL, _("All statuses")),
        ],
    )

    def __init__(self, *args: Any, default_assignee: str = ASSIGNEE_ALL, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.default_assignee = default_assignee
        assignee = self.fields["assignee"]
        assert isinstance(assignee, forms.ChoiceField)
        assignee.choices = [
            (ASSIGNEE_ALL, _("All staff")),
            (ASSIGNEE_MINE, _("Assigned to me")),
            (ASSIGNEE_NONE, _("Not assigned")),
            *[(str(member.pk), member.name) for member in active_staff()],
        ]
        for bound_field in self.fields.values():
            bound_field.widget.attrs["class"] = "select"

    def selected_assignee(self) -> str:
        value = self.data.get("assignee")
        return str(value) if value else self.default_assignee

    def to_query(self, user: User) -> dashboard.DashboardQuery:
        self.is_valid()
        data = self.cleaned_data
        assignee = self.selected_assignee()
        assignee_id: int | None = None
        if assignee == ASSIGNEE_MINE:
            own = own_staff_id(user)
            assignee_id = own if own is not None else 0  # 担当者として未登録なら 0 件
        elif assignee.isdigit():
            assignee_id = int(assignee)
        level = data.get("level")
        return dashboard.DashboardQuery(
            assignee_id=assignee_id,
            unassigned_only=assignee == ASSIGNEE_NONE,
            level=deadlines.Level(level) if level else None,
            kind=data.get("kind") or "",
            filer=data.get("filer") or "",
            statuses=STATUS_GROUPS.get(data.get("status") or STATUS_OPEN, dashboard.OPEN_STATUSES),
        )


class TransitionForm(StyledFormMixin, forms.Form):
    """状態の変更。行き先は今の状態から行けるものだけ（T11 の TRANSITIONS）。"""

    to = forms.ChoiceField(label=_("new status"))
    submitted_on = forms.DateField(label=_("submitted on"), required=False, widget=forms.DateInput)
    receipt_number = forms.CharField(label=_("receipt number"), required=False, max_length=50)
    reason = forms.CharField(
        label=_("reason"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text=_("Required when the task is not required, cancelled, or reopened."),
    )

    def __init__(self, *args: Any, filing: Filing, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.filing = filing
        to = self.fields["to"]
        assert isinstance(to, forms.ChoiceField)
        to.choices = [
            (status.value, status.label)
            for status in FilingStatus
            if status in filings.allowed_transitions(filing)
        ]
        self.initial.setdefault("submitted_on", filing.submitted_on)
        self.initial.setdefault("receipt_number", filing.receipt_number)
        self.style_fields()

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        if "to" in cleaned:
            try:
                filings.validate_transition(
                    self.filing,
                    FilingStatus(cleaned["to"]),
                    submitted_on=cleaned.get("submitted_on"),
                    receipt_number=cleaned.get("receipt_number") or "",
                    reason=cleaned.get("reason") or "",
                )
            except ValidationError as error:
                field = {
                    "submitted_on": "submitted_on",
                    "receipt_number": "receipt_number",
                    "reason": "reason",
                }.get(error.code or "", None)
                self.add_error(field, error)
        return cleaned


class DetailsForm(StyledFormMixin, forms.ModelForm[Filing]):
    """担当者・メモ（提出済み以降は提出日・受理番号の書き直しも）。"""

    class Meta:
        model = Filing
        fields = ("assignee", "note", "submitted_on", "receipt_number")
        widgets = {"submitted_on": forms.DateInput, "note": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        assignee = self.fields["assignee"]
        assert isinstance(assignee, forms.ModelChoiceField)
        assignee.queryset = active_staff()
        if self.instance.status in dashboard.OPEN_STATUSES:
            # 提出日・受理番号は状態の変更（提出済・受理済）で入れる
            del self.fields["submitted_on"]
            del self.fields["receipt_number"]
        self.style_fields()


class RebaseForm(StyledFormMixin, forms.Form):
    """起算日（事由の日）の訂正。期限を計算し直す（T11 の rebase）。"""

    basis_on = forms.DateField(label=_("corrected basis date"), widget=forms.DateInput)
    reason = forms.CharField(label=_("reason for correction"), max_length=200)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()


class ManualFilingForm(StyledFormMixin, forms.Form):
    """手動の起票。対象は外国人・雇用契約・受入れ機関のちょうど 1 つ。期限は種別から計算する。"""

    kind = forms.ChoiceField(
        label=_("kind"), choices=[(k.value, k.label) for k in filings.MANUAL_KINDS]
    )
    basis_on = forms.DateField(
        label=pgettext_lazy("filing", "basis date"),
        widget=forms.DateInput,
        help_text=_("The date the event occurred."),
    )
    worker = forms.ModelChoiceField(
        label=_("foreign worker"), queryset=ForeignWorker.objects.none(), required=False
    )
    contract = forms.ModelChoiceField(
        label=_("employment contract"), queryset=EmploymentContract.objects.none(), required=False
    )
    host = forms.ModelChoiceField(
        label=_("host organization"), queryset=HostOrganization.objects.none(), required=False
    )
    assignee = forms.ModelChoiceField(
        label=_("assignee"),
        queryset=Staff.objects.none(),
        required=False,
        help_text=_("If blank, the staff member assigned to the foreign worker is used."),
    )
    note = forms.CharField(
        label=_("note"), required=False, widget=forms.Textarea(attrs={"rows": 3})
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        active = workers.active_workers()
        self.fields["worker"].queryset = active.order_by("name_romaji")  # type: ignore[attr-defined]
        self.fields["contract"].queryset = (  # type: ignore[attr-defined]
            EmploymentContract.objects.filter(worker__in=active)
            .select_related("worker", "host_organization")
            .order_by("worker__name_romaji", "-current_starts_on")
        )
        self.fields["host"].queryset = HostOrganization.objects.order_by("name_kana", "name")  # type: ignore[attr-defined]
        self.fields["assignee"].queryset = active_staff()  # type: ignore[attr-defined]
        self.style_fields()

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        try:
            cleaned["target"] = filings.manual_target(
                worker=cleaned.get("worker"),
                contract=cleaned.get("contract"),
                host=cleaned.get("host"),
            )
        except ValidationError as error:
            self.add_error(None, error)
        return cleaned
