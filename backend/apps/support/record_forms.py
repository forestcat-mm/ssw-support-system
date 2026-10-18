"""支援実績・交付記録の画面のフォーム（T18〜T22）。型の検査だけを行い、記録の検査はサービス（services）。"""

from __future__ import annotations

import datetime
from typing import Any

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.contracts.models import EmploymentContract
from apps.core.models import Staff, SupportLanguage
from apps.core.web_forms import StyledFormMixin
from apps.forms_engine.models import GeneratedDocument
from apps.support.forms import StaffChoiceField, active_staff
from apps.support.models import (
    OPEN_CONSULTATION_STATUSES,
    BodyLanguage,
    CheckStatus,
    ConsultationCategory,
    ConsultationStatus,
    Counterpart,
    DeliveredBy,
    DeliveryKind,
    DeliveryMethod,
    DepositContract,
    ForeignFees,
    RecordMethod,
    ReportDestination,
    WorkerGuidance,
)
from apps.workers.models import ForeignWorker
from services import briefings, deliveries, interview_answers, support_records
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
        allowed = methods if methods is not None else support_records.methods_for(kind)
        method = self.fields["method"]
        assert isinstance(method, forms.ChoiceField)
        method.choices = [
            (value, label) for value, label in RecordMethod.choices if value in allowed
        ]
        if len(allowed) == 1:  # 送迎・公的手続への同行は対面で固定し、選ばせない（ADR-0014）
            method.disabled = True
            method.initial = allowed[0]
            method.help_text = _("This kind of support uses only this method.")
        if RecordMethod.ONLINE not in allowed:
            del self.fields["meeting_url"]
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
        # 違反の疑いが「有」のときだけ要否を、要否が「要」のときだけ通報先・通報日を入力できる
        self.enable_when("report_required", violation_suspected="yes")
        for name in ("report_to", "report_to_note", "reported_on"):
            self.enable_when(name, violation_suspected="yes", report_required="yes")


PROBLEM_CHOICES = [("no", _("no problem")), ("yes", _("problem"))]


class AnswerSetForm(StyledFormMixin, forms.Form):
    """1 行（本人用・監督者用）の報告書の質問（第5-5号・第5-6号の ３ と ⑦。T35、ADR-0024）。

    質問ごとに「問題の有無」（answer_<符号>）と「問題の内容」（detail_<符号>。「有」のときだけ）。
    質問の文言は、本人用は第5-5号、監督者用は第5-6号のもの。
    """

    other_subject = forms.CharField(
        label=_("other matter (5-2)"),
        max_length=200,
        required=False,
        help_text=_('Required when "other" has a problem.'),
    )
    notable = forms.CharField(
        label=_("other matters worth noting (7)"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
    )
    supervisor_position = forms.CharField(
        label=_("position of the supervisor"), max_length=100, required=False
    )
    supervisor_department = forms.CharField(
        label=_("department of the supervisor"), max_length=100, required=False
    )
    recording_location = forms.CharField(
        label=_("where the recording is stored"),
        max_length=200,
        required=False,
        help_text=_("Required for online interviews (the recording is kept for a year or more)."),
    )

    def __init__(self, *args: Any, supervisor: bool, on: datetime.date, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.supervisor = supervisor
        self.version = interview_answers.version_for(on)
        if not supervisor:
            del self.fields["supervisor_position"]
            del self.fields["supervisor_department"]
        for question in self.version.questions:
            key = question.code.replace(".", "_")
            text = question.supervisor_text if supervisor else question.self_text
            self.fields[f"answer_{key}"] = forms.ChoiceField(
                label=f"{question.code}　{text}",
                choices=PROBLEM_CHOICES,
                widget=forms.RadioSelect,
                required=False,
            )
            self.fields[f"detail_{key}"] = forms.CharField(
                label=_("details of the problem"),
                required=False,
                widget=forms.Textarea(attrs={"rows": 2}),
            )
        self.style_fields()
        for question in self.version.questions:
            key = question.code.replace(".", "_")
            self.enable_when(f"detail_{key}", **{f"answer_{key}": "yes"})

    def question_fields(self) -> list[tuple[Any, Any]]:
        return [
            (self[f"answer_{q.code.replace('.', '_')}"], self[f"detail_{q.code.replace('.', '_')}"])
            for q in self.version.questions
        ]

    def to_input(self) -> interview_answers.AnswerSetInput:
        data = self.cleaned_data
        answers = {}
        for question in self.version.questions:
            key = question.code.replace(".", "_")
            value = data.get(f"answer_{key}") or ""
            answers[question.code] = interview_answers.AnswerInput(
                None if not value else value == "yes", data.get(f"detail_{key}") or ""
            )
        return interview_answers.AnswerSetInput(
            answers=answers,
            other_subject=data.get("other_subject") or "",
            notable=data.get("notable") or "",
            supervisor_position=data.get("supervisor_position") or "",
            supervisor_department=data.get("supervisor_department") or "",
            recording_location=data.get("recording_location") or "",
        )

    def apply_errors(self, error: ValidationError) -> None:
        for key, messages in error.error_dict.items():
            name = key
            for prefix in ("answer.", "detail."):
                if key.startswith(prefix):
                    name = prefix[:-1] + "_" + key[len(prefix) :].replace(".", "_")
            self.add_error(name if name in self.fields else None, messages)


class NonconformityForm(StyledFormMixin, forms.Form):
    """⑥ 基準不適合等の有無と ４ 基準不適合等への対応（組に 1 つ。T35-D4）。"""

    exists = forms.ChoiceField(
        label=_("non-conformity with the standards (6)"),
        choices=YES_NO,
        widget=forms.RadioSelect,
        required=False,
        help_text=_('"Yes" when a violation of labour laws is suspected.'),
    )
    occurred_on = forms.DateField(
        label=_("date of the non-conformity"), required=False, widget=forms.DateInput
    )
    content = forms.CharField(
        label=_("details of the non-conformity"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
    )
    worker_guidance = forms.ChoiceField(
        label=_("action for the worker"),
        choices=WorkerGuidance.choices,
        widget=forms.RadioSelect,
        required=False,
    )
    guided_to = forms.CharField(
        label=_("authority the worker was guided to"), max_length=200, required=False
    )
    no_guidance_reason = forms.CharField(
        label=_("reason for no particular action"), max_length=500, required=False
    )
    host_notified = forms.ChoiceField(
        label=_("notified the person responsible at the host"),
        choices=[("yes", _("notified")), ("no", _("not notified"))],
        widget=forms.RadioSelect,
        required=False,
    )
    notified_on = forms.DateField(label=_("notified on"), required=False, widget=forms.DateInput)
    notified_to = forms.CharField(label=_("notified to"), max_length=200, required=False)
    not_notified_reason = forms.CharField(
        label=_("reason for not notifying"), max_length=500, required=False
    )
    immigration_guided = forms.ChoiceField(
        label=_("guided the host to the Immigration Services Agency"),
        choices=[("yes", _("guided")), ("no", _("not yet"))],
        widget=forms.RadioSelect,
        required=False,
    )
    unreported_reason = forms.CharField(
        label=_("reason the report to authorities is not made"),
        max_length=500,
        required=False,
        help_text=_("Including when the report is judged not needed."),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()
        for name in (
            "occurred_on",
            "content",
            "worker_guidance",
            "host_notified",
            "immigration_guided",
            "unreported_reason",
        ):
            self.enable_when(name, exists="yes")
        self.enable_when("guided_to", exists="yes", worker_guidance=WorkerGuidance.GUIDED)
        self.enable_when("no_guidance_reason", exists="yes", worker_guidance=WorkerGuidance.NONE)
        self.enable_when("notified_on", exists="yes", host_notified="yes")
        self.enable_when("notified_to", exists="yes", host_notified="yes")
        self.enable_when("not_notified_reason", exists="yes", host_notified="no")

    def to_input(self) -> interview_answers.NonconformityInput:
        data = self.cleaned_data

        def yes_no(name: str) -> bool | None:
            value = data.get(name) or ""
            return None if not value else value == "yes"

        return interview_answers.NonconformityInput(
            exists=yes_no("exists"),
            occurred_on=data.get("occurred_on"),
            content=data.get("content") or "",
            worker_guidance=data.get("worker_guidance") or "",
            guided_to=data.get("guided_to") or "",
            no_guidance_reason=data.get("no_guidance_reason") or "",
            host_notified=yes_no("host_notified"),
            notified_on=data.get("notified_on"),
            notified_to=data.get("notified_to") or "",
            not_notified_reason=data.get("not_notified_reason") or "",
            immigration_guided=yes_no("immigration_guided"),
            unreported_reason=data.get("unreported_reason") or "",
        )


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
        # 質問の版は実施日で決まる（入力の前は今日）。2 行の日付が違っても、今の質問の版は 1 つ
        on = timezone.localdate()
        self.self_answers = AnswerSetForm(data, prefix="self_answers", supervisor=False, on=on)
        self.supervisor_answers = AnswerSetForm(
            data, prefix="supervisor_answers", supervisor=True, on=on
        )
        self.nonconformity_form = NonconformityForm(data, prefix="nonconformity")

    def forms(self) -> list[forms.Form]:
        return [
            self.self_form,
            self.self_answers,
            self.supervisor_form,
            self.supervisor_answers,
            self.interview_form,
            self.nonconformity_form,
        ]

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
            self_answers=self.self_answers.to_input(),
            supervisor_answers=self.supervisor_answers.to_input(),
            nonconformity=self.nonconformity_form.to_input(),
        )

    def files(self) -> dict[str, list[Any]]:
        return {
            "self": self.self_form.cleaned_data.get("uploads") or [],
            "supervisor": self.supervisor_form.cleaned_data.get("uploads") or [],
        }

    def apply_errors(self, error: ValidationError) -> None:
        """サービスのエラー（"self.body"・"report_to" など）を該当する入力欄に戻す。"""
        rows = {"self": self.self_form, "supervisor": self.supervisor_form}
        answers = {"self": self.self_answers, "supervisor": self.supervisor_answers}
        answer_keys = ("answer", "detail", "other_subject", "recording_location")
        for key, messages in error.error_dict.items():
            prefix, _dot, name = key.partition(".")
            if prefix in answers and name.split(".")[0] in answer_keys:
                answers[prefix].apply_errors(ValidationError({name: messages}))
            elif prefix in rows and name:
                rows[prefix].apply_errors(ValidationError({name: messages}))
            elif prefix == "nonconformity" and name in self.nonconformity_form.fields:
                self.nonconformity_form.add_error(name, messages)
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
        # 「通報」のときだけ通報先・通報日を、「ハローワーク相談」のときだけ相談日を入力できる
        for name in ("report_to", "report_to_note", "reported_on"):
            self.enable_when(name, reported="on")
        self.enable_when("hello_work_on", hello_work="on")

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


# --- 事前ガイダンス・生活オリエンテーション（T21）---------------------------------------------


class SessionForm(StyledFormMixin, forms.Form):
    """実施日時の 1 行（確認書の「年月日 時分から時分まで」）。"""

    occurred_on = forms.DateField(label=_("date"), widget=forms.DateInput)
    start_time = forms.TimeField(
        label=_("starts at"), widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M")
    )
    end_time = forms.TimeField(
        label=_("ends at"), widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M")
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()


class FeeForm(StyledFormMixin, forms.Form):
    """外国の機関に支払った費用の 1 行。"""

    organization_name = forms.CharField(label=_("name of the organization abroad"), max_length=200)
    paid_on = forms.DateField(label=_("paid on"), widget=forms.DateInput)
    amount_jpy = forms.IntegerField(label=_("amount (converted into yen)"), min_value=1)
    breakdown = forms.CharField(
        label=_("breakdown"),
        max_length=500,
        help_text=_("Example: placement fee 150,000 yen, training fee 50,000 yen."),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()


SessionFormSet = forms.formset_factory(SessionForm, extra=0, min_num=1)
FeeFormSet = forms.formset_factory(FeeForm, extra=1)

AGREED_CHOICES = [("yes", _("understood and agreed")), ("no", _("not agreed"))]
CHECK_CHOICES = [
    ("", _("not checked")),
    (CheckStatus.DONE.value, CheckStatus.DONE.label),
    (CheckStatus.NOT_APPLICABLE.value, CheckStatus.NOT_APPLICABLE.label),
]


def _check_name(code: str) -> str:
    return "check_" + code.replace(".", "_")


def _reason_name(code: str) -> str:
    return "reason_" + code.replace(".", "_")


class BriefingForm(StyledFormMixin, forms.Form):
    """事前ガイダンス・生活オリエンテーションの欄（実施日時・費用の行は別の formset）。

    細目のチェックは check_<符号>（未・説明した・該当なし）と reason_<符号>（該当なしの理由）。
    """

    method = forms.ChoiceField(label=_("method"), choices=(), widget=forms.RadioSelect)
    meeting_url = forms.URLField(
        label=_("online meeting URL"), required=False, max_length=500, assume_scheme="https"
    )
    identity_confirmed = forms.BooleanField(
        label=_("identity of the worker confirmed"),
        required=False,
        help_text=_("Required for online guidance (video call)."),
    )
    staff = StaffChoiceField(label=_("explained by"), queryset=Staff.objects.none())
    place = forms.CharField(
        label=_("place"),
        max_length=200,
        help_text=_("Example: meeting room of the host organization / online (our office)."),
    )
    languages = forms.ModelMultipleChoiceField(
        label=_("languages used"),
        queryset=SupportLanguage.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    interpreter = forms.CharField(
        label=_("interpreter (name and affiliation)"),
        max_length=200,
        required=False,
        help_text=_("Only when someone other than the staff in charge interpreted."),
    )
    legal_info_provider = forms.CharField(
        label=_("explained legal protection (name and affiliation)"),
        max_length=200,
        required=False,
        help_text=_("Only when someone other than the staff in charge explained item 6."),
    )
    continuing_employment = forms.BooleanField(
        label=_("continuing at the same organization without a change in living environment"),
        required=False,
        help_text=_(
            "Example: a technical intern or student who continues at the same organization. "
            "Only the minimum time is checked."
        ),
    )
    deposit_contract = forms.ChoiceField(
        label=_("contract on a deposit or penalty (declared by the worker)"),
        choices=DepositContract.choices,
        widget=forms.RadioSelect,
        required=False,
    )
    deposit_collector = forms.CharField(
        label=_("organization collecting the deposit or managing property"),
        max_length=200,
        required=False,
    )
    deposit_detail = forms.CharField(
        label=_("deposit amount or property managed"), max_length=200, required=False
    )
    foreign_fees = forms.ChoiceField(
        label=_("fees paid to organizations abroad (placement, preparation)"),
        choices=ForeignFees.choices,
        widget=forms.RadioSelect,
        required=False,
    )
    fees_agreed = forms.ChoiceField(
        label=_("understood and agreed on the amount and breakdown"),
        choices=AGREED_CHOICES,
        widget=forms.RadioSelect,
        required=False,
    )
    body_language = forms.ChoiceField(
        label=_("language of the details"), choices=BodyLanguage.choices
    )
    body = forms.CharField(
        label=_("notes"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text=_("Optional. Example: questions from the worker and the answers."),
    )
    signed_on = forms.DateField(
        label=_("date of signature"),
        required=False,
        widget=forms.DateInput,
        help_text=_("Enter when attaching the signed confirmation."),
    )
    confirmation = MultipleFileField(
        label=_("signed confirmation (scan)"),
        required=False,
        help_text=_("PDF, PNG or JPEG, up to 10 MB each. You can also attach it later."),
    )

    MAIN = (
        "method",
        "meeting_url",
        "identity_confirmed",
        "staff",
        "place",
        "languages",
        "interpreter",
        "legal_info_provider",
        "continuing_employment",
    )
    DEPOSIT = ("deposit_contract", "deposit_collector", "deposit_detail")
    FEES = ("foreign_fees", "fees_agreed")
    BODY = ("body_language", "body")
    SIGNATURE = ("signed_on", "confirmation")
    GUIDANCE_ONLY = ("identity_confirmed", *DEPOSIT, *FEES)

    def __init__(self, *args: Any, kind: SupportItem, checklist: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.kind = kind
        self.checklist = checklist
        method = self.fields["method"]
        assert isinstance(method, forms.ChoiceField)
        allowed = briefings.METHODS[kind]
        method.choices = [(v, label) for v, label in RecordMethod.choices if v in allowed]
        if kind == SupportItem.PRE_GUIDANCE:
            del self.fields["legal_info_provider"]
        else:
            for name in self.GUIDANCE_ONLY:
                del self.fields[name]
        self.fields["staff"].queryset = active_staff()  # type: ignore[attr-defined]
        self.fields["languages"].queryset = SupportLanguage.objects.filter(  # type: ignore[attr-defined]
            is_active=True
        )
        for item in checklist.items:
            self.fields[_check_name(item.code)] = forms.ChoiceField(
                label=item.label, choices=CHECK_CHOICES, required=False, widget=forms.RadioSelect
            )
            self.fields[_reason_name(item.code)] = forms.CharField(
                label=_("reason for not applicable"), max_length=200, required=False
            )
        self.style_fields()

    @property
    def is_guidance(self) -> bool:
        return self.kind == SupportItem.PRE_GUIDANCE

    def group(self, names: tuple[str, ...]) -> list[forms.BoundField]:
        return [self[name] for name in names if name in self.fields]

    def main_fields(self) -> list[forms.BoundField]:
        return self.group(self.MAIN)

    def deposit_fields(self) -> list[forms.BoundField]:
        return self.group(self.DEPOSIT)

    def fee_fields(self) -> list[forms.BoundField]:
        return self.group(self.FEES)

    def body_fields(self) -> list[forms.BoundField]:
        return self.group(self.BODY)

    def signature_fields(self) -> list[forms.BoundField]:
        return self.group(self.SIGNATURE)

    def checklist_rows(self) -> list[tuple[Any, list[tuple[Any, Any, Any]]]]:
        """欄ごとの（欄, [(細目, チェックの欄, 理由の欄)]）。"""
        return [
            (
                section,
                [
                    (item, self[_check_name(item.code)], self[_reason_name(item.code)])
                    for item in section.items
                ],
            )
            for section in self.checklist.sections
        ]

    def checks(self) -> dict[str, briefings.CheckInput]:
        data = self.cleaned_data
        result = {}
        for code in self.checklist.item_codes:
            status = data.get(_check_name(code)) or ""
            if status:
                result[code] = briefings.CheckInput(
                    status=status, reason=data.get(_reason_name(code)) or ""
                )
        return result

    def field_for_error(self, key: str) -> str | None:
        """サービスのエラーのキー（"checks.4.a.1" など）から入力欄の名前。"""
        if key.startswith("checks."):
            code = key.removeprefix("checks.")
            name = _reason_name(code)
            return name if name in self.fields else None
        return key if key in self.fields else None


class BriefingEditor:
    """事前ガイダンス・生活オリエンテーションの画面のフォーム（欄・実施日時の行・費用の行）を束ねる。"""

    def __init__(
        self, worker: ForeignWorker, kind: SupportItem, data: Any = None, files: Any = None
    ) -> None:
        today = timezone.localdate()
        self.kind = kind
        self.checklist = briefings.checklist_for(kind, today)
        language = worker.support_language
        initial = {
            "method": RecordMethod.IN_PERSON,
            "staff": worker.assigned_staff_id,
            "languages": [language.pk] if language and language.is_active else [],
            "body_language": support_records.body_language_for(worker),
        }
        self.form = BriefingForm(data, files, kind=kind, checklist=self.checklist, initial=initial)
        self.sessions = SessionFormSet(
            data, prefix="sessions", initial=None if data is not None else [{"occurred_on": today}]
        )
        self.fees = FeeFormSet(data, prefix="fees") if kind == SupportItem.PRE_GUIDANCE else None
        self._session_forms: list[SessionForm] = []
        self._fee_forms: list[FeeForm] = []

    def formsets(self) -> list[Any]:
        return [formset for formset in (self.sessions, self.fees) if formset is not None]

    def is_valid(self) -> bool:
        return all([self.form.is_valid(), *[formset.is_valid() for formset in self.formsets()]])

    @staticmethod
    def _filled(formset: Any) -> list[Any]:
        """入力のある行（空のまま足した行は除く）。"""
        return [form for form in formset.forms if form.has_changed() and form.cleaned_data]

    def to_input(self) -> briefings.BriefingInput:
        data = self.form.cleaned_data
        self._session_forms = self._filled(self.sessions)
        self._fee_forms = self._filled(self.fees) if self.fees is not None else []
        agreed = data.get("fees_agreed")
        return briefings.BriefingInput(
            kind=self.kind,
            method=data["method"],
            meeting_url=data.get("meeting_url") or "",
            staff=data["staff"],
            place=data["place"],
            languages=list(data.get("languages") or []),
            interpreter=data.get("interpreter") or "",
            legal_info_provider=data.get("legal_info_provider") or "",
            continuing_employment=bool(data.get("continuing_employment")),
            identity_confirmed=bool(data.get("identity_confirmed")),
            sessions=[
                briefings.SessionInput(
                    occurred_on=form.cleaned_data["occurred_on"],
                    start_time=form.cleaned_data["start_time"],
                    end_time=form.cleaned_data["end_time"],
                )
                for form in self._session_forms
            ],
            checks=self.form.checks(),
            deposit_contract=data.get("deposit_contract") or "",
            deposit_collector=data.get("deposit_collector") or "",
            deposit_detail=data.get("deposit_detail") or "",
            foreign_fees=data.get("foreign_fees") or "",
            fees=[
                briefings.FeeInput(
                    organization_name=form.cleaned_data["organization_name"],
                    paid_on=form.cleaned_data["paid_on"],
                    amount_jpy=form.cleaned_data["amount_jpy"],
                    breakdown=form.cleaned_data["breakdown"],
                )
                for form in self._fee_forms
            ],
            fees_agreed=None if not agreed else agreed == "yes",
            body=data.get("body") or "",
            body_language=data["body_language"],
            signed_on=data.get("signed_on"),
        )

    def confirmations(self) -> list[Any]:
        return list(self.form.cleaned_data.get("confirmation") or [])

    def apply_errors(self, error: ValidationError) -> None:
        """サービスのエラー（"sessions.0.end_time"・"checks.4.a.1" など）を該当する入力欄に戻す。"""
        rows: dict[str, list[Any]] = {"sessions": self._session_forms, "fees": self._fee_forms}
        for key, messages in error.error_dict.items():
            prefix, _dot, rest = key.partition(".")
            if prefix in rows and rest:
                index, _dot, name = rest.partition(".")
                forms_ = rows[prefix]
                if index.isdigit() and int(index) < len(forms_):
                    form = forms_[int(index)]
                    form.add_error(name if name in form.fields else None, messages)
                    continue
            if key in ("sessions", "fees"):
                self.form.add_error(None, messages)
                continue
            self.form.add_error(self.form.field_for_error(key), messages)


class ConfirmationForm(StyledFormMixin, forms.Form):
    """署名済みの確認書を後から添付する（署名日は必須）。"""

    signed_on = forms.DateField(label=_("date of signature"), widget=forms.DateInput)
    confirmation = MultipleFileField(
        label=_("signed confirmation (scan)"),
        help_text=_("PDF, PNG or JPEG, up to 10 MB each. You can select more than one."),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()

    def apply_errors(self, error: ValidationError) -> None:
        if not hasattr(error, "error_dict"):
            self.add_error(None, error)
            return
        for key, messages in error.error_dict.items():
            self.add_error(key if key in self.fields else None, messages)


# --- 交付記録（T22）----------------------------------------------------------------------


class ContractChoiceField(forms.ModelChoiceField):  # type: ignore[type-arg]
    def label_from_instance(self, obj: Any) -> str:
        return f"{obj.host_organization}（{obj.current_starts_on:%Y-%m-%d}〜）"


class DeliveryForm(StyledFormMixin, forms.Form):
    """交付記録の入力（種別は URL で決める。当該契約・求めを受けた日は種別で出し分ける）。"""

    title = forms.CharField(label=_("name of the document"), max_length=200, required=False)
    delivered_on = forms.DateField(label=_("delivered on"), widget=forms.DateInput)
    method = forms.ChoiceField(
        label=_("how delivered"), choices=DeliveryMethod.choices, widget=forms.RadioSelect
    )
    method_note = forms.CharField(
        label=_("how delivered (details)"),
        max_length=200,
        required=False,
        help_text=_('Required when "other" is selected.'),
    )
    delivered_by = forms.ChoiceField(
        label=_("delivered by"), choices=DeliveredBy.choices, widget=forms.RadioSelect
    )
    delivered_by_name = forms.CharField(
        label=_("name of the person who delivered it"), max_length=100, required=False
    )
    contract = ContractChoiceField(
        label=_("employment contract"), queryset=EmploymentContract.objects.none()
    )
    requested_on = forms.DateField(
        label=_("requested on"),
        required=False,
        widget=forms.DateInput,
        help_text=_("Optional. The date the worker asked for the certificate."),
    )
    explained = forms.BooleanField(label=_("explained the content"), required=False)
    languages = forms.ModelMultipleChoiceField(
        label=_("languages of the document"),
        queryset=SupportLanguage.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    note = forms.CharField(
        label=_("notes"), required=False, widget=forms.Textarea(attrs={"rows": 3})
    )
    uploads = MultipleFileField(
        label=_("signed copy or receipt (scan)"),
        required=False,
        help_text=_("PDF, PNG or JPEG, up to 10 MB each. You can also attach it later."),
    )
    generated = forms.IntegerField(required=False, widget=forms.HiddenInput)
    consented = forms.BooleanField(
        label=_("obtained consent to the career path including the scope of job changes"),
        required=False,
        help_text=_("Career-up plan for the seafood processing category (from 1 April 2027)."),
    )

    def __init__(self, *args: Any, worker: ForeignWorker, kind: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.kind = kind
        self.worker = worker
        contract = self.fields["contract"]
        assert isinstance(contract, ContractChoiceField)
        contract.queryset = (
            EmploymentContract.objects.filter(worker=worker)
            .select_related("host_organization")
            .order_by("-current_starts_on")
        )
        if kind in deliveries.CONTRACT_KINDS:
            contract.help_text = _("The contract this document belongs to.")
        else:
            contract.required = False
            if kind == DeliveryKind.SUPPORT_PLAN_COPY:
                del self.fields["contract"]
        if kind != DeliveryKind.EXPERIENCE_CERTIFICATE:
            del self.fields["requested_on"]
        title = self.fields["title"]
        if kind == DeliveryKind.OTHER:
            title.required = True
            title.help_text = _("Example: company housing agreement, pledge of the host.")
        else:
            title.help_text = _("Optional. Example: the version or the title on the document.")
        if kind == DeliveryKind.CAREER_UP_PLAN:
            self.fields["delivered_on"].help_text = _(
                "Give it before the employment contract is concluded."
            )
        else:
            del self.fields["consented"]
        self.fields["languages"].queryset = SupportLanguage.objects.filter(  # type: ignore[attr-defined]
            is_active=True
        )
        self.style_fields()

    @classmethod
    def initial_for(cls, worker: ForeignWorker, today: Any) -> dict[str, Any]:
        contract = support_records.contract_on(worker, today)
        language_ids = list(
            SupportLanguage.objects.filter(code="ja", is_active=True).values_list("pk", flat=True)
        )
        language = worker.support_language
        if language and language.is_active and language.pk not in language_ids:
            language_ids.append(language.pk)
        return {
            "delivered_on": today,
            "method": DeliveryMethod.HAND,
            "delivered_by": DeliveredBy.ORGANIZATION,
            "contract": contract.pk if contract else None,
            "languages": language_ids,
        }

    def to_input(self) -> deliveries.DeliveryInput:
        data = self.cleaned_data
        return deliveries.DeliveryInput(
            kind=self.kind,
            title=data.get("title") or "",
            delivered_on=data["delivered_on"],
            method=data["method"],
            method_note=data.get("method_note") or "",
            delivered_by=data["delivered_by"],
            delivered_by_name=data.get("delivered_by_name") or "",
            explained=bool(data.get("explained")),
            contract=data.get("contract"),
            requested_on=data.get("requested_on"),
            languages=list(data.get("languages") or []),
            note=data.get("note") or "",
            generated_document=self.generated_document(),
            consented=bool(data.get("consented")),
        )

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        pk = data.get("generated")
        if pk and self.generated_document() is None:
            self.add_error(None, _("The generated document was not found."))
        return data

    def generated_document(self) -> GeneratedDocument | None:
        """元の生成物（この外国人の生成物だけを引く。中身の検査は deliveries.validate）。"""
        pk = self.cleaned_data.get("generated")
        if not pk:
            return None
        return (
            GeneratedDocument.objects.select_related("template")
            .filter(pk=pk, worker=self.worker)
            .first()
        )

    def apply_errors(self, error: ValidationError) -> None:
        for key, messages in error.error_dict.items():
            name = "uploads" if key == "files" else key
            self.add_error(name if name in self.fields else None, messages)
