"""外国人の業務画面のフォーム（入力の検査だけ。検索・判定は services に置く）。"""

import datetime
from typing import Any

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.core.forms import allow_formatted_number_input
from apps.core.models import AttachmentKind, Staff, User
from apps.core.web_forms import DateSelectWidget, StyledFormMixin
from apps.hosts.models import HostOrganization, SpecifiedSkillField
from apps.workers.models import (
    ForeignWorker,
    InternshipCompletion,
    PassportRecord,
    QualificationArea,
    ResidenceRecord,
    StatusOfResidence,
    WorkerAddress,
    WorkerContact,
    WorkerQualification,
    WorkerRelative,
    WorkerStatus,
    WorkHistory,
)
from services import attachments, deadlines, immigration_codes, workers
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

    include_deactivated = forms.BooleanField(label=_("Include deactivated"), required=False)

    def __init__(self, *args: Any, can_see_deactivated: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if not can_see_deactivated:
            del self.fields["include_deactivated"]
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
            if name not in ("q", "include_deactivated"):
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
            include_deactivated=bool(data.get("include_deactivated")),
        )


# --- 登録・編集（T13d）。フォームは項目の型だけを検査し、業務のルールは services.workers ---


def apply_service_errors(form: forms.BaseForm, error: ValidationError) -> None:
    """services の ValidationError（項目ごと）をフォームの項目のエラーにする。"""
    if not hasattr(error, "error_dict"):
        form.add_error(None, error)
        return
    for name, messages in error.error_dict.items():
        for message in messages:
            form.add_error(name if name in form.fields else None, message)


class WorkerForm(StyledFormMixin, forms.ModelForm[ForeignWorker]):
    """基本情報（新規登録・編集）。編集では開いた時点の更新日時で上書きの衝突を検出する。"""

    # 開いた時点の更新日時（ISO 形式。秒未満まで比べるため文字列で持つ）
    loaded_updated_at = forms.CharField(widget=forms.HiddenInput, required=False)

    class Meta:
        model = ForeignWorker
        fields = (
            "name_romaji",
            "name_kanji",
            "name_native",
            "name_kana",
            "nationality",
            "birth_date",
            "sex",
            "support_language",
            "assigned_staff",
            "status",
            "birthplace",
            "has_relatives_in_japan",
            "has_work_history",
            "ssw_months_outside_ledger",
        )
        widgets = {"birth_date": DateSelectWidget}  # 遠い日付は年・月・日の選択（修正 1 回目 B16）

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()
        self.normalize_field("name_kana", "kana")  # 外国人の氏名のカナはカタカナ（修正 1 回目 A4）
        if self.instance.pk is not None:
            # 担当者は「担当者を変える」で、開始日と理由を入れて変える（履歴。T45-D4）
            del self.fields["assigned_staff"]
        else:
            staff_field = self.fields["assigned_staff"]
            assert isinstance(staff_field, forms.ModelChoiceField)
            staff_field.queryset = Staff.objects.filter(is_active=True).order_by(
                "name_kana", "name"
            )
        if self.instance.pk is not None and not self.is_bound:
            self.initial["loaded_updated_at"] = self.instance.updated_at.isoformat()

    def clean_name_kana(self) -> str:
        return workers.clean_name_kana(self.cleaned_data.get("name_kana") or "")

    def clean_loaded_updated_at(self) -> datetime.datetime | None:
        value = self.cleaned_data.get("loaded_updated_at") or ""
        try:
            return datetime.datetime.fromisoformat(value) if value else None
        except ValueError:
            return None


class HistoryRecordForm(StyledFormMixin, forms.ModelForm[Any]):
    """履歴行の追加（1 操作 1 画面。新規登録では区分ごとに任意で入れる）。"""

    def __init__(self, *args: Any, worker_status: str | None = None, **kwargs: Any) -> None:
        self.worker_status = worker_status
        super().__init__(*args, **kwargs)
        self.style_fields()

    def worker_id(self) -> int | None:
        return getattr(self.instance, "worker_id", None)


class ResidenceRecordForm(HistoryRecordForm):
    class Meta:
        model = ResidenceRecord
        fields = (
            "application_type",
            "status_of_residence",
            "technical_intern_type",
            "card_number",
            "permitted_on",
            "expires_on",
            "period_of_stay",
        )
        widgets = {"permitted_on": forms.DateInput, "expires_on": forms.DateInput}

    period_of_stay = forms.ChoiceField(
        label=_("period of stay"),
        required=False,
        help_text=_("The length written on the residence card (for example, 1 year)."),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        period = self.fields["period_of_stay"]
        assert isinstance(period, forms.ChoiceField)
        period.choices = immigration_codes.choices(immigration_codes.PERIOD_OF_STAY)
        allow_formatted_number_input(self.fields["card_number"])
        self.fields["card_number"].widget.attrs.update(
            {"class": "input mono", "autocomplete": "off", "spellcheck": "false"}
        )

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        if cleaned.get("technical_intern_type") and cleaned.get("status_of_residence") not in (
            StatusOfResidence.TECHNICAL_INTERN_2,
            StatusOfResidence.TECHNICAL_INTERN_3,
        ):
            self.add_error(
                "technical_intern_type",
                _("Choose this only for Technical Intern Training (ii) and (iii)."),
            )
        if "card_number" in cleaned:
            try:
                cleaned.update(
                    workers.clean_residence(
                        cleaned, worker_id=self.worker_id(), worker_status=self.worker_status
                    )
                )
            except ValidationError as error:
                apply_service_errors(self, error)
        return cleaned


class PassportRecordForm(HistoryRecordForm):
    class Meta:
        model = PassportRecord
        fields = ("number", "issuing_country", "issued_on", "expires_on")
        widgets = {"issued_on": forms.DateInput, "expires_on": forms.DateInput}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        allow_formatted_number_input(self.fields["number"])
        self.fields["number"].widget.attrs.update(
            {"class": "input mono", "autocomplete": "off", "spellcheck": "false"}
        )
        self.normalize_field("number", "passport")

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        if "number" in cleaned:
            try:
                cleaned.update(workers.clean_passport(cleaned, worker_id=self.worker_id()))
            except ValidationError as error:
                apply_service_errors(self, error)
        return cleaned


class WorkerAddressForm(HistoryRecordForm):
    class Meta:
        model = WorkerAddress
        fields = ("postal_code", "address", "effective_from")
        widgets = {"effective_from": forms.DateInput}


class WorkerContactForm(HistoryRecordForm):
    class Meta:
        model = WorkerContact
        fields = (
            "phone",
            "email",
            "emergency_name",
            "emergency_relationship",
            "emergency_phone",
            "home_country_address",
            "home_country_phone",
            "effective_from",
        )
        widgets = {"effective_from": forms.DateInput}


def _code_field(label: Any, code: str, *, required: bool = True, help_text: Any = "") -> Any:
    """一括申請ツールのコードの選択肢（T46-D3）。"""
    return forms.ChoiceField(
        label=label,
        required=required,
        choices=lambda: immigration_codes.choices(code),
        help_text=help_text,
    )


def _first_of_month(value: datetime.date | None) -> datetime.date | None:
    return value.replace(day=1) if value is not None else None


class WorkerRelativeForm(HistoryRecordForm):
    """在日親族・同居者（T46a）。"""

    relationship = _code_field(_("relationship"), immigration_codes.RELATIONSHIP)
    nationality_code = _code_field(_("nationality or region"), immigration_codes.COUNTRY)

    class Meta:
        model = WorkerRelative
        fields = (
            "relationship",
            "name",
            "birth_date",
            "nationality_code",
            "lives_together",
            "workplace",
            "card_number",
        )
        widgets = {"birth_date": DateSelectWidget}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        allow_formatted_number_input(self.fields["card_number"])
        self.fields["card_number"].widget.attrs.update(
            {"class": "input mono", "autocomplete": "off", "spellcheck": "false"}
        )
        self.fields["card_number"].help_text = _("Optional. 12 characters.")


class CertificateFormMixin(HistoryRecordForm):
    """証明書（PDF・画像）を 1 つ添付できる行（任意）。"""

    certificate = forms.FileField(label=_("certificate (scan)"), required=False)
    certificate_kind: AttachmentKind

    def clean_certificate(self) -> Any:
        file = self.cleaned_data.get("certificate")
        if file:
            attachments.validate_upload(self.certificate_kind, file)
        return file

    def files_to_attach(self) -> list[tuple[AttachmentKind, Any]]:
        file = self.cleaned_data.get("certificate")
        return [(self.certificate_kind, file)] if file else []


class WorkerQualificationForm(CertificateFormMixin):
    """技能水準・日本語能力の評価（試験の合格など。T46a）。"""

    certificate_kind = AttachmentKind.QUALIFICATION_CERTIFICATE
    evaluation = _code_field(_("evaluation method"), immigration_codes.EVALUATION)
    exam = forms.ChoiceField(label=_("test passed"), required=False)
    exam_country = _code_field(
        _("country or region where the test was taken"), immigration_codes.COUNTRY, required=False
    )

    class Meta:
        model = WorkerQualification
        fields = (
            "area",
            "evaluation",
            "exam",
            "exam_country",
            "passed_on",
            "level",
            "other_proof",
        )
        widgets = {"passed_on": forms.DateInput}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        exam = self.fields["exam"]
        assert isinstance(exam, forms.ChoiceField)
        # 技能の試験（CC09）と日本語の試験（CC10）。コードが重なるので、分野ごとに見出しを付ける
        exam.choices = [
            ("", "---------"),
            (
                str(_("skill level")),
                immigration_codes.choices(immigration_codes.SKILL_EXAM, blank=False),
            ),
            (
                str(_("Japanese language ability")),
                [
                    (f"ja:{value}", label)
                    for value, label in immigration_codes.choices(
                        immigration_codes.JAPANESE_EXAM, blank=False
                    )
                ],
            ),
        ]
        self.fields["level"].help_text = _("For example, N4 or A2.")

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        exam = cleaned.get("exam") or ""
        area = cleaned.get("area")
        if exam.startswith("ja:"):
            if area != QualificationArea.JAPANESE:
                self.add_error("exam", _("Choose a test of the same area."))
            cleaned["exam"] = exam.removeprefix("ja:")
        elif exam and area != QualificationArea.SKILL:
            self.add_error("exam", _("Choose a test of the same area."))
        return cleaned


class InternshipCompletionForm(CertificateFormMixin):
    """良好に修了した技能実習2号（T46a）。作業は職種ごとの表にあるものだけ。"""

    certificate_kind = AttachmentKind.INTERNSHIP_CERTIFICATE
    occupation = _code_field(_("job category"), immigration_codes.INTERN_OCCUPATION)
    work = _code_field(_("operation"), immigration_codes.INTERN_WORK)
    proof = _code_field(_("proof of good completion"), immigration_codes.INTERN_PROOF)

    class Meta:
        model = InternshipCompletion
        fields = ("occupation", "work", "proof", "completed_on")
        widgets = {"completed_on": forms.DateInput}

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        occupation, work = cleaned.get("occupation"), cleaned.get("work")
        if occupation and work:
            works = {
                value
                for value, _label in immigration_codes.linked(
                    immigration_codes.INTERN_OCCUPATION, occupation
                )
            }
            if work not in works:
                self.add_error("work", _("This operation is not in the chosen job category."))
        return cleaned


class WorkHistoryForm(HistoryRecordForm):
    """職歴（T46a）。年月は月の 1 日で持つ。"""

    class Meta:
        model = WorkHistory
        fields = ("joined_on", "left_on", "employer")
        widgets = {"joined_on": forms.DateInput, "left_on": forms.DateInput}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["joined_on"].help_text = _("Only the year and month are used.")
        self.fields["left_on"].help_text = _("Leave blank if still working there.")

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        joined = _first_of_month(cleaned.get("joined_on"))
        left = _first_of_month(cleaned.get("left_on"))
        cleaned["joined_on"], cleaned["left_on"] = joined, left
        if joined and left and left < joined:
            self.add_error("left_on", _("The month of leaving is before the month of joining."))
        return cleaned


# 履歴の区分 → 追加のフォーム。キーは URL の <kind> と services.worker_detail の区分と同じ
RECORD_FORMS: dict[str, type[HistoryRecordForm]] = {
    "residence": ResidenceRecordForm,
    "passport": PassportRecordForm,
    "address": WorkerAddressForm,
    "contact": WorkerContactForm,
    "relative": WorkerRelativeForm,
    "qualification": WorkerQualificationForm,
    "internship": InternshipCompletionForm,
    "work": WorkHistoryForm,
}


class VoidForm(StyledFormMixin, forms.Form):
    reason = forms.CharField(
        label=_("void reason"),
        max_length=200,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("Example: the card number was entered incorrectly."),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()


class DeactivateForm(StyledFormMixin, forms.Form):
    reason = forms.CharField(
        label=_("deactivation reason"),
        max_length=200,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("Example: registered twice by mistake."),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()


class ReactivateForm(StyledFormMixin, forms.Form):
    reason = forms.CharField(
        label=_("reason for cancelling the deactivation"),
        max_length=200,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()
