from datetime import date
from typing import Any

from django import forms
from django.contrib import admin, messages
from django.http import HttpRequest
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.utils.safestring import SafeString
from django.utils.translation import gettext_lazy as _

from apps.contracts.models import EmploymentContract
from apps.core.admin import TRACKED_READONLY_FIELDS, TrackedModelAdmin
from apps.core.admin_filters import DateRangeFilter
from apps.core.models import User
from apps.hosts.models import HostOrganization
from apps.workers.models import ForeignWorker
from services import deadlines, filings

from .models import Filing, FilingKind, FilingStatus, Trigger

LEVEL_LABELS = {
    deadlines.Level.NOT_YET: _("not yet open"),
    deadlines.Level.OPEN: _("open"),
    deadlines.Level.WITHIN_14: _("within 14 days"),
    deadlines.Level.WITHIN_7: _("within 7 days"),
    deadlines.Level.OVERDUE: _("overdue"),
}
CATEGORY_LABELS = filings.CATEGORY_LABELS
FILER_LABELS = filings.FILER_LABELS


def _actor(request: HttpRequest) -> User | None:
    return request.user if isinstance(request.user, User) else None


class _DefinitionFilter(admin.SimpleListFilter):
    """種別の定義（区別・届出主体）で絞り込む。"""

    attribute = ""
    labels: dict[Any, Any] = {}

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, Any]]:
        return [(str(key), label) for key, label in self.labels.items()]

    def queryset(self, request: HttpRequest, queryset: Any) -> Any:
        value = self.value()
        if not value:
            return queryset
        kinds = [
            kind
            for kind, definition in filings.KIND_DEFINITIONS.items()
            if getattr(definition, self.attribute) == value
        ]
        return queryset.filter(kind__in=kinds)


class CategoryFilter(_DefinitionFilter):
    title = _("category")
    parameter_name = "category"
    attribute = "category"
    labels = CATEGORY_LABELS


class FilerFilter(_DefinitionFilter):
    title = _("filer")
    parameter_name = "filer"
    attribute = "filer"
    labels = FILER_LABELS


class FilingForm(forms.ModelForm[Filing]):
    """状態の変更・提出日・受理番号・理由・担当者・メモと、起算日の訂正。"""

    new_basis_on = forms.DateField(
        label=_("corrected basis date"),
        required=False,
        help_text=_(
            "Correct the basis date (e.g. the date of the event) and recalculate the due date."
        ),
    )
    rebase_reason = forms.CharField(label=_("reason for correction"), required=False)

    class Meta:
        model = Filing
        fields = ("status", "submitted_on", "receipt_number", "status_reason", "assignee", "note")

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        stored = Filing.objects.get(pk=self.instance.pk)
        to = FilingStatus(cleaned.get("status") or stored.status)
        try:
            if to != stored.status:
                filings.validate_transition(
                    stored,
                    to,
                    submitted_on=cleaned.get("submitted_on"),
                    receipt_number=cleaned.get("receipt_number") or "",
                    reason=cleaned.get("status_reason") or "",
                )
            if cleaned.get("new_basis_on") and not (cleaned.get("rebase_reason") or "").strip():
                raise forms.ValidationError(_("Enter the reason for correction."))
        except forms.ValidationError as error:
            self.add_error(None, error)
        return cleaned


class ManualFilingForm(forms.ModelForm[Filing]):
    """手動の起票。対象は外国人・雇用契約・受入れ機関のどれか 1 つ。期限は種別から計算する。"""

    target_worker = forms.ModelChoiceField(
        label=_("foreign worker"), queryset=ForeignWorker.objects.all(), required=False
    )
    target_contract = forms.ModelChoiceField(
        label=_("employment contract"), queryset=EmploymentContract.objects.all(), required=False
    )
    target_host = forms.ModelChoiceField(
        label=_("host organization"), queryset=HostOrganization.objects.all(), required=False
    )

    class Meta:
        model = Filing
        fields = ("kind", "basis_on", "assignee", "note")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        kind_field = self.fields["kind"]
        assert isinstance(kind_field, forms.ChoiceField)
        kind_field.choices = [(k.value, k.label) for k in filings.MANUAL_KINDS]
        self.fields["basis_on"].help_text = _("The date the event occurred.")

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        targets = [
            t
            for t in (
                cleaned.get("target_contract"),
                cleaned.get("target_worker"),
                cleaned.get("target_host"),
            )
            if t is not None
        ]
        if len(targets) != 1:
            self.add_error(None, _("Select exactly one target."))
        else:
            cleaned["target"] = targets[0]
        return cleaned


@admin.register(Filing)
class FilingAdmin(TrackedModelAdmin[Filing]):
    form = FilingForm
    list_display = (
        "kind",
        "category",
        "filer",
        "target_repr",
        "worker",
        "unadjusted_due_on",
        "due_on",
        "level",
        "status",
        "assignee",
        "needs_review",
    )
    list_filter = (
        "status",
        "kind",
        CategoryFilter,
        FilerFilter,
        ("unadjusted_due_on", DateRangeFilter),
        "assignee",
        "needs_review",
    )
    search_fields = (
        "target_repr",
        "receipt_number",
        "worker__name_romaji",
        "worker__name_kanji",
    )
    list_select_related = ("worker", "assignee")
    readonly_fields = (
        "kind",
        "category",
        "filer",
        "target_repr",
        "worker",
        "trigger",
        "trigger_summary",
        "needs_review",
        "basis_on",
        "opens_on",
        "unadjusted_due_on",
        "due_on",
        "final_on",
        "level",
        *TRACKED_READONLY_FIELDS,
    )
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "kind",
                    "category",
                    "filer",
                    "target_repr",
                    "worker",
                    "trigger",
                    "trigger_summary",
                    "needs_review",
                )
            },
        ),
        (
            _("Deadline"),
            {
                "fields": (
                    "basis_on",
                    "opens_on",
                    "unadjusted_due_on",
                    "due_on",
                    "final_on",
                    "level",
                    "new_basis_on",
                    "rebase_reason",
                )
            },
        ),
        (
            _("Status"),
            {
                "fields": (
                    "status",
                    "submitted_on",
                    "receipt_number",
                    "status_reason",
                    "assignee",
                    "note",
                )
            },
        ),
        (_("Record"), {"fields": TRACKED_READONLY_FIELDS}),
    )

    def get_form(
        self, request: HttpRequest, obj: Any = None, change: bool = False, **kwargs: Any
    ) -> Any:
        if obj is None:
            kwargs["form"] = ManualFilingForm
            kwargs["fields"] = None
        return super().get_form(request, obj, change, **kwargs)

    def get_fieldsets(self, request: HttpRequest, obj: Any = None) -> Any:
        if obj is None:
            return (
                (
                    None,
                    {
                        "fields": (
                            "kind",
                            "basis_on",
                            "target_contract",
                            "target_worker",
                            "target_host",
                        )
                    },
                ),
                (_("Status"), {"fields": ("assignee", "note")}),
            )
        return super().get_fieldsets(request, obj)

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> Any:
        return () if obj is None else self.readonly_fields

    @admin.display(description=_("category"))
    def category(self, obj: Filing) -> str:
        return str(CATEGORY_LABELS[filings.definition(obj.kind).category])

    @admin.display(description=_("filer"))
    def filer(self, obj: Filing) -> str:
        return str(FILER_LABELS[filings.definition(obj.kind).filer])

    @admin.display(description=_("level"))
    def level(self, obj: Filing) -> str:
        if obj.status not in filings.OPEN_STATUSES:
            return "-"
        result = deadlines.assess(filings.deadline_of(obj), timezone.localdate())
        return f"{LEVEL_LABELS[result.level]}（{result.days_left}）"

    @admin.display(description=_("trigger details"))
    def trigger_summary(self, obj: Filing) -> SafeString | str:
        details = obj.trigger_details or {}
        rows = [(_("required"), f["item"], f["message"]) for f in details.get("required", [])]
        rows += [(_("needs review"), f["item"], f["message"]) for f in details.get("review", [])]
        if not rows:
            return "-"
        return format_html(
            "<table><tbody>{}</tbody></table>",
            format_html_join("", "<tr><td>{}</td><td>{}</td><td>{}</td></tr>", rows),
        )

    def save_model(self, request: HttpRequest, obj: Filing, form: Any, change: bool) -> None:
        actor = _actor(request)
        if not change:
            filing, created = filings.create(
                FilingKind(form.cleaned_data["kind"]),
                target=form.cleaned_data["target"],
                basis_on=form.cleaned_data["basis_on"],
                trigger=Trigger.MANUAL,
                by=actor,
                assignee=form.cleaned_data.get("assignee"),
                note=form.cleaned_data.get("note") or "",
            )
            if not created:
                self.message_user(request, _("The same task already exists."), messages.WARNING)
            obj.__dict__.update(filing.__dict__)
            return
        stored = Filing.objects.get(pk=obj.pk)
        if (stored.assignee_id, stored.note) != (obj.assignee_id, obj.note):
            stored.assignee_id = obj.assignee_id
            stored.note = obj.note
            stored.updated_by = actor
            stored.save(update_fields=["assignee", "note", "updated_by", "updated_at"])
        new_basis_on: date | None = form.cleaned_data.get("new_basis_on")
        if new_basis_on:
            filings.rebase(
                stored, new_basis_on, by=actor, reason=form.cleaned_data["rebase_reason"]
            )
        if obj.status != stored.status:
            filings.transition(
                stored,
                FilingStatus(obj.status),
                by=actor,
                submitted_on=obj.submitted_on,
                receipt_number=obj.receipt_number,
                reason=obj.status_reason,
            )
        elif (stored.submitted_on, stored.receipt_number) != (obj.submitted_on, obj.receipt_number):
            stored.submitted_on = obj.submitted_on
            stored.receipt_number = obj.receipt_number
            stored.updated_by = actor
            stored.save(
                update_fields=["submitted_on", "receipt_number", "updated_by", "updated_at"]
            )

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False
