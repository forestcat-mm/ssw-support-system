"""支援実績の画面のフォーム（T18）。型の検査だけを行い、記録の検査は services.support_records。"""

from __future__ import annotations

from typing import Any

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import Staff, SupportLanguage
from apps.core.web_forms import StyledFormMixin
from apps.support.forms import StaffChoiceField, active_staff
from apps.support.models import (
    OPEN_CONSULTATION_STATUSES,
    BodyLanguage,
    ConsultationCategory,
    ConsultationStatus,
    Counterpart,
    RecordMethod,
    ReportDestination,
)
from apps.workers.models import ForeignWorker
from services import support_records
from services.consultations import CONSULTATION_METHODS, EntryInput, ReceptionInput
from services.support_catalog import SupportItem
from services.support_interviews import INTERVIEW_METHODS, InterviewInput
from services.support_records import DETAIL_FIELDS, RecordInput

DETAIL_PREFIX = "detail_"


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """複数のファイルを 1 つの欄で受け取る（中身の検査は services.attachments）。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("widget", MultipleFileInput)
        super().__init__(*args, **kwargs)

    def clean(self, data: Any, initial: Any = None) -> Any:
        if isinstance(data, (list, tuple)):
            return [super(MultipleFileField, self).clean(item, initial) for item in data if item]
        return [super().clean(data, initial)] if data else []


def _detail_field(spec: support_records.DetailField) -> forms.Field:
    """種別ごとの欄（DETAIL_FIELDS から作る。中身の検査はサービスでも行う）。"""
    field: forms.Field
    if spec.kind == "multi":
        field = forms.MultipleChoiceField(
            label=spec.label, choices=spec.choices, widget=forms.CheckboxSelectMultiple
        )
    elif spec.kind == "choice":
        field = forms.ChoiceField(label=spec.label, choices=[("", "---------"), *spec.choices])
    else:
        field = forms.CharField(label=spec.label, max_length=spec.max_length)
    field.required = spec.required
    return field


class RecordForm(StyledFormMixin, forms.Form):
    """記録の入力（共通の欄＋種別ごとの欄＋添付）。"""

    occurred_on = forms.DateField(label=_("date of support"), widget=forms.DateInput)
    start_time = forms.TimeField(
        label=_("time started"),
        required=False,
        widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
    )
    duration_minutes = forms.IntegerField(
        label=_("duration (minutes)"), required=False, min_value=1
    )
    method = forms.ChoiceField(
        label=_("method"), choices=RecordMethod.choices, widget=forms.RadioSelect
    )
    meeting_url = forms.URLField(
        label=_("online meeting URL"), required=False, max_length=500, assume_scheme="https"
    )
    staff = StaffChoiceField(label=_("staff in charge"), queryset=Staff.objects.none())
    counterpart = forms.ChoiceField(
        label=_("counterpart"), choices=Counterpart.choices, widget=forms.RadioSelect
    )
    counterpart_name = forms.CharField(
        label=_("name of the supervisor"),
        required=False,
        max_length=100,
        help_text=_("Only when the counterpart is the supervisor. Optional."),
    )
    languages = forms.ModelMultipleChoiceField(
        label=_("languages used"),
        queryset=SupportLanguage.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    body_language = forms.ChoiceField(
        label=_("language of the details"), choices=BodyLanguage.choices
    )
    body = forms.CharField(label=_("details"), widget=forms.Textarea(attrs={"rows": 6}))
    uploads = MultipleFileField(
        label=_("attachments"),
        required=False,
        help_text=_("PDF, PNG or JPEG, up to 10 MB each. You can select more than one."),
    )

    COMMON = (
        "occurred_on",
        "start_time",
        "duration_minutes",
        "method",
        "meeting_url",
        "staff",
        "counterpart",
        "counterpart_name",
        "languages",
    )
    BODY = ("body_language", "body")

    def __init__(
        self,
        *args: Any,
        kind: SupportItem,
        counterpart: str | None = None,
        methods: tuple[str, ...] | None = None,
        **kwargs: Any,
    ) -> None:
        """counterpart を渡すと相手を固定する（定期面談の本人用・上司用の行。上司は氏名が必須）。"""
        super().__init__(*args, **kwargs)
        self.kind = kind
        self.fixed_counterpart = counterpart
        if counterpart is not None:
            del self.fields["counterpart"]
            if counterpart == Counterpart.SUPERVISOR:
                name = self.fields["counterpart_name"]
                name.required = True
                name.help_text = ""
            else:
                del self.fields["counterpart_name"]
        allowed = methods if methods is not None else support_records.DEFAULT_METHODS
        method = self.fields["method"]
        assert isinstance(method, forms.ChoiceField)
        method.choices = [
            (value, label) for value, label in RecordMethod.choices if value in allowed
        ]
        self.fields["staff"].queryset = active_staff()  # type: ignore[attr-defined]
        self.fields["languages"].queryset = SupportLanguage.objects.filter(  # type: ignore[attr-defined]
            is_active=True
        )
        for spec in DETAIL_FIELDS.get(kind, ()):
            self.fields[DETAIL_PREFIX + spec.name] = _detail_field(spec)
        self.style_fields()

    @classmethod
    def initial_for(cls, worker: ForeignWorker, today: Any) -> dict[str, Any]:
        """初期値（今日・外国人の担当者・支援言語・本文の言語・相手は本人）。"""
        language = worker.support_language
        return {
            "occurred_on": today,
            "staff": worker.assigned_staff_id,
            "method": RecordMethod.IN_PERSON,
            "counterpart": Counterpart.WORKER,
            "languages": [language.pk] if language and language.is_active else [],
            "body_language": support_records.body_language_for(worker),
        }

    def common_fields(self) -> list[forms.BoundField]:
        return [self[name] for name in self.COMMON if name in self.fields]

    def detail_fields(self) -> list[forms.BoundField]:
        return [self[name] for name in self.fields if name.startswith(DETAIL_PREFIX)]

    def body_fields(self) -> list[forms.BoundField]:
        return [self[name] for name in self.BODY]

    def to_input(self) -> RecordInput:
        data = self.cleaned_data
        return RecordInput(
            kind=self.kind,
            occurred_on=data["occurred_on"],
            start_time=data.get("start_time"),
            duration_minutes=data.get("duration_minutes"),
            method=data["method"],
            meeting_url=data.get("meeting_url") or "",
            staff=data["staff"],
            counterpart=self.fixed_counterpart or data["counterpart"],
            counterpart_name=data.get("counterpart_name") or "",
            languages=list(data.get("languages") or []),
            body=data["body"],
            body_language=data["body_language"],
            details={
                name.removeprefix(DETAIL_PREFIX): value
                for name, value in data.items()
                if name.startswith(DETAIL_PREFIX)
            },
        )

    def apply_errors(self, error: ValidationError) -> None:
        """サービスのエラー（"details.airport" など）を該当する入力欄に戻す。"""
        for key, messages in error.error_dict.items():
            name = key.replace("details.", DETAIL_PREFIX, 1) if key.startswith("details.") else key
            name = "uploads" if name == "files" else name
            if name == "__all__" or name not in self.fields:
                self.add_error(None, messages)
            else:
                self.add_error(name, messages)


class ReasonForm(StyledFormMixin, forms.Form):
    """取消の理由（必須）。"""

    reason = forms.CharField(
        label=_("void reason"), max_length=200, widget=forms.Textarea(attrs={"rows": 3})
    )

    def __init__(self, *args: Any, help_text: Any = "", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["reason"].help_text = help_text
        self.style_fields()


class AttachmentForm(StyledFormMixin, forms.Form):
    uploads = MultipleFileField(
        label=_("attachments"),
        help_text=_("PDF, PNG or JPEG, up to 10 MB each. You can select more than one."),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()


class RecordFilterForm(StyledFormMixin, forms.Form):
    """一覧の絞り込み（種別・期間・担当者）。"""

    kind = forms.ChoiceField(label=_("kind of support"), required=False)
    date_from = forms.DateField(
        label=_("date of support (from)"), required=False, widget=forms.DateInput
    )
    date_to = forms.DateField(
        label=_("date of support (to)"), required=False, widget=forms.DateInput
    )
    staff = forms.ModelChoiceField(
        label=_("staff in charge"), queryset=Staff.objects.none(), required=False
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["kind"].choices = [  # type: ignore[attr-defined]
            ("", _("All")),
            *[(kind.value, kind.label) for kind in SupportItem],
        ]
        self.fields["staff"].queryset = Staff.objects.order_by(  # type: ignore[attr-defined]
            "name_kana", "name"
        )
        self.style_fields()


# --- 定期面談（T19）---------------------------------------------------------------------

YES_NO = [("no", _("no")), ("yes", _("yes"))]
REQUIRED_CHOICES = [("yes", _("report needed")), ("no", _("report not needed"))]


class InterviewForm(StyledFormMixin, forms.Form):
    """定期面談の組の欄（労働法令違反の疑い・通報・次回の予定日）。"""

    violation_suspected = forms.ChoiceField(
        label=_("suspected violation of labour laws"),
        choices=YES_NO,
        widget=forms.RadioSelect,
        initial="no",
    )
    report_required = forms.ChoiceField(
        label=_("report"),
        choices=REQUIRED_CHOICES,
        widget=forms.RadioSelect,
        required=False,
        help_text=_("Only when a violation is suspected."),
    )
    report_to = forms.ChoiceField(
        label=_("report to"),
        choices=[("", "---------"), *ReportDestination.choices],
        required=False,
    )
    report_to_note = forms.CharField(
        label=_("report to (details)"),
        max_length=200,
        required=False,
        help_text=_('Required when "other" is selected. Example: the name of the office.'),
    )
    reported_on = forms.DateField(label=_("reported on"), required=False, widget=forms.DateInput)
    next_scheduled_on = forms.DateField(
        label=_("next interview scheduled on"),
        required=False,
        widget=forms.DateInput,
        help_text=_(
            "Optional. The next task is due on this date (within 3 months). "
            "If empty, it is due 3 months after the interview."
        ),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()


class InterviewEditor:
    """定期面談の画面のフォーム（本人用の行・上司用の行・組の欄）を束ねる。"""

    def __init__(self, worker: ForeignWorker, data: Any = None, files: Any = None) -> None:
        initial = RecordForm.initial_for(worker, timezone.localdate())
        supervisor_initial = initial | {
            "languages": list(
                SupportLanguage.objects.filter(code="ja", is_active=True).values_list(
                    "pk", flat=True
                )
            ),
            "body_language": BodyLanguage.JA,
        }
        kind = SupportItem.PERIODIC_INTERVIEW
        self.self_form = RecordForm(
            data,
            files,
            prefix="self",
            kind=kind,
            counterpart=Counterpart.WORKER,
            methods=INTERVIEW_METHODS,
            initial=initial,
        )
        self.supervisor_form = RecordForm(
            data,
            files,
            prefix="supervisor",
            kind=kind,
            counterpart=Counterpart.SUPERVISOR,
            methods=INTERVIEW_METHODS,
            initial=supervisor_initial,
        )
        self.interview_form = InterviewForm(data, prefix="interview")

    def forms(self) -> list[forms.Form]:
        return [self.self_form, self.supervisor_form, self.interview_form]

    def is_valid(self) -> bool:
        return all([form.is_valid() for form in self.forms()])

    def to_input(self) -> InterviewInput:
        data = self.interview_form.cleaned_data
        required = data.get("report_required")
        return InterviewInput(
            self_row=self.self_form.to_input(),
            supervisor_row=self.supervisor_form.to_input(),
            violation_suspected=data["violation_suspected"] == "yes",
            report_required=None if not required else required == "yes",
            report_to=data.get("report_to") or "",
            report_to_note=data.get("report_to_note") or "",
            reported_on=data.get("reported_on"),
            next_scheduled_on=data.get("next_scheduled_on"),
        )

    def files(self) -> dict[str, list[Any]]:
        return {
            "self": self.self_form.cleaned_data.get("uploads") or [],
            "supervisor": self.supervisor_form.cleaned_data.get("uploads") or [],
        }

    def apply_errors(self, error: ValidationError) -> None:
        """サービスのエラー（"self.body"・"report_to" など）を該当する入力欄に戻す。"""
        rows = {"self": self.self_form, "supervisor": self.supervisor_form}
        for key, messages in error.error_dict.items():
            prefix, _dot, name = key.partition(".")
            if prefix in rows and name:
                rows[prefix].apply_errors(ValidationError({name: messages}))
            elif key in self.interview_form.fields:
                self.interview_form.add_error(key, messages)
            else:
                self.interview_form.add_error(None, messages)


# --- 相談・苦情対応（T20）-----------------------------------------------------------------


def _status_field(label: Any) -> forms.ChoiceField:
    return forms.ChoiceField(label=label, choices=ConsultationStatus.choices)


class ReceptionForm(RecordForm):
    """相談の受付（支援実績の共通の欄＋主な分類・対応状況）。相手は本人に固定。"""

    CASE = ("category", "category_note", "status", "handed_over_to")

    category = forms.ChoiceField(
        label=_("main category"),
        choices=[("", "---------"), *ConsultationCategory.choices],
        help_text=_("If it spans several categories, select the main one."),
    )
    category_note = forms.CharField(
        label=_("category (details)"),
        max_length=100,
        required=False,
        help_text=_('Required when "other" is selected.'),
    )
    status = _status_field(_("consultation status"))
    handed_over_to = forms.CharField(
        label=_("handed over to"),
        max_length=200,
        required=False,
        help_text=_("Required when the case is handed over to another organization."),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(
            *args,
            kind=SupportItem.CONSULTATION,
            counterpart=Counterpart.WORKER,
            methods=CONSULTATION_METHODS,
            **kwargs,
        )

    def case_fields(self) -> list[forms.BoundField]:
        return [self[name] for name in self.CASE]

    def to_reception(self) -> ReceptionInput:
        data = self.cleaned_data
        return ReceptionInput(
            record=self.to_input(),
            category=data["category"],
            category_note=data.get("category_note") or "",
            status=data["status"],
            handed_over_to=data.get("handed_over_to") or "",
        )


class EntryForm(StyledFormMixin, forms.Form):
    """対応の経過の追記（追記のたびに状態・通報・ハローワーク相談を変えられる）。"""

    occurred_on = forms.DateField(label=_("date of support"), widget=forms.DateInput)
    start_time = forms.TimeField(
        label=_("time started"),
        required=False,
        widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
    )
    method = forms.ChoiceField(
        label=_("method"),
        choices=[(v, label) for v, label in RecordMethod.choices if v in CONSULTATION_METHODS],
        widget=forms.RadioSelect,
    )
    staff = StaffChoiceField(label=_("staff in charge"), queryset=Staff.objects.none())
    status_after = _status_field(_("status after this entry"))
    handed_over_to = forms.CharField(
        label=_("handed over to"),
        max_length=200,
        required=False,
        help_text=_("Required when the case is handed over to another organization."),
    )
    body_language = forms.ChoiceField(
        label=_("language of the details"), choices=BodyLanguage.choices
    )
    body = forms.CharField(
        label=_("details"),
        widget=forms.Textarea(attrs={"rows": 5}),
        help_text=_('To correct an earlier entry, add an entry that starts with "Correction:".'),
    )
    reported = forms.BooleanField(label=_("reported to authorities"), required=False)
    report_to = forms.ChoiceField(
        label=_("report to"),
        choices=[("", "---------"), *ReportDestination.choices],
        required=False,
    )
    report_to_note = forms.CharField(label=_("report to (details)"), max_length=200, required=False)
    reported_on = forms.DateField(label=_("reported on"), required=False, widget=forms.DateInput)
    hello_work = forms.BooleanField(label=_("consulted Hello Work"), required=False)
    hello_work_on = forms.DateField(
        label=_("consulted Hello Work on"), required=False, widget=forms.DateInput
    )

    MAIN = ("occurred_on", "start_time", "method", "staff", "status_after", "handed_over_to")
    BODY = ("body_language", "body")
    FLAGS = (
        "reported",
        "report_to",
        "report_to_note",
        "reported_on",
        "hello_work",
        "hello_work_on",
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["staff"].queryset = active_staff()  # type: ignore[attr-defined]
        self.style_fields()

    @classmethod
    def initial_for(cls, consultation: Any, today: Any) -> dict[str, Any]:
        return {
            "occurred_on": today,
            "method": consultation.record.method,
            "staff": consultation.record.staff_id,
            "status_after": consultation.status,
            "handed_over_to": consultation.handed_over_to,
            "body_language": consultation.record.body_language,
        }

    def group(self, names: tuple[str, ...]) -> list[forms.BoundField]:
        return [self[name] for name in names]

    def main_fields(self) -> list[forms.BoundField]:
        return self.group(self.MAIN)

    def body_fields(self) -> list[forms.BoundField]:
        return self.group(self.BODY)

    def flag_fields(self) -> list[forms.BoundField]:
        return self.group(self.FLAGS)

    def to_input(self) -> EntryInput:
        data = self.cleaned_data
        return EntryInput(
            occurred_on=data["occurred_on"],
            start_time=data.get("start_time"),
            method=data["method"],
            staff=data["staff"],
            body=data["body"],
            body_language=data["body_language"],
            status_after=data["status_after"],
            handed_over_to=data.get("handed_over_to") or "",
            reported=bool(data.get("reported")),
            report_to=data.get("report_to") or "",
            report_to_note=data.get("report_to_note") or "",
            reported_on=data.get("reported_on"),
            hello_work=bool(data.get("hello_work")),
            hello_work_on=data.get("hello_work_on"),
        )

    def apply_errors(self, error: ValidationError) -> None:
        for key, messages in error.error_dict.items():
            self.add_error(key if key in self.fields else None, messages)


STATUS_OPEN = ""
STATUS_FILTERS = {
    STATUS_OPEN: (_("Unresolved"), OPEN_CONSULTATION_STATUSES),
    "resolved": (_("Resolved"), (ConsultationStatus.RESOLVED,)),
    "handed_over": (_("Handed over"), (ConsultationStatus.HANDED_OVER,)),
    "all": (_("All"), ()),
}


class ConsultationFilterForm(StyledFormMixin, forms.Form):
    """相談の一覧の絞り込み（既定は未解決＝受付・対応中）。"""

    status = forms.ChoiceField(
        label=_("consultation status"),
        required=False,
        choices=[(key, label) for key, (label, _statuses) in STATUS_FILTERS.items()],
    )
    category = forms.ChoiceField(
        label=_("main category"),
        required=False,
        choices=[("", _("All")), *ConsultationCategory.choices],
    )
    staff = forms.ModelChoiceField(
        label=_("staff in charge"), queryset=Staff.objects.none(), required=False
    )
    date_from = forms.DateField(
        label=_("received on (from)"), required=False, widget=forms.DateInput
    )
    date_to = forms.DateField(label=_("received on (to)"), required=False, widget=forms.DateInput)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["staff"].queryset = Staff.objects.order_by(  # type: ignore[attr-defined]
            "name_kana", "name"
        )
        self.style_fields()

    def statuses(self) -> tuple[str, ...]:
        key = self.cleaned_data.get("status") or STATUS_OPEN if self.is_bound else STATUS_OPEN
        return tuple(STATUS_FILTERS.get(key, STATUS_FILTERS[STATUS_OPEN])[1])
