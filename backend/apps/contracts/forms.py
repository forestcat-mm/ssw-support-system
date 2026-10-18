"""雇用契約の業務画面のフォーム（T28b）。検査の中身は services.contracts（validate_version）。

版の欄は雇用条件書（参考様式第1-6号、001472154.docx。ADR-0016）の Ⅰ〜Ⅸ と別紙1 の順に並べる。
"""

from __future__ import annotations

from typing import Any

from django import forms
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.contracts.models import (
    ContractWageExplanation,
    EmploymentContract,
    EmploymentContractAllowance,
    EmploymentContractDeduction,
    EmploymentContractShift,
    EmploymentContractVersion,
    TerminationReason,
)
from apps.core.web_forms import StyledFormMixin
from apps.hosts.models import HostSite, OperationCategory
from apps.support.record_forms import MultipleFileField
from services import contracts

SECTIONS: tuple[tuple[Any, tuple[str, ...]], ...] = (
    (_("Change"), ("changed_on", "change_reason", "signed_on")),
    (
        _("I. Contract period"),
        (
            "starts_on",
            "ends_on",
            "expected_entry_on",
            "renewal",
            "renewal_criteria",
            "renewal_criteria_other",
            "has_renewal_limit",
            "renewal_limit_count",
            "renewal_limit_years",
            "indefinite_conversion",
            "conversion_conditions_change",
        ),
    ),
    (_("II. Workplace"), ("site", "workplace_change_scope")),
    (_("III. Duties"), ("categories", "duties", "duty_change_scope")),
    (
        _("IV. Working hours"),
        (
            "start_time",
            "end_time",
            "daily_working_minutes",
            "variable_hours_unit",
            "break_minutes",
            "weekly_working_minutes",
            "monthly_working_minutes",
            "annual_working_minutes",
            "weekly_working_days",
            "monthly_working_days",
            "annual_working_days",
            "has_overtime",
            "work_rules_articles_hours",
        ),
    ),
    (
        _("V. Holidays"),
        (
            "regular_holiday_weekdays",
            "national_holidays_off",
            "regular_holidays_other",
            "annual_holidays",
            "irregular_holidays",
            "work_rules_articles_holidays",
        ),
    ),
    (
        _("VI. Leave"),
        (
            "paid_leave_days",
            "has_early_paid_leave",
            "early_paid_leave_months",
            "early_paid_leave_days",
            "other_paid_leave",
            "other_unpaid_leave",
            "temporary_return_leave_note",
            "work_rules_articles_leave",
        ),
    ),
    (
        _("VII. Wages"),
        (
            "wage_type",
            "base_wage",
            "hourly_equivalent",
            "monthly_equivalent",
            "overtime_rate_within_60h",
            "overtime_rate_over_60h",
            "overtime_rate_scheduled",
            "holiday_rate_statutory",
            "holiday_rate_non_statutory",
            "night_rate",
            "wage_closing_day",
            "wage_payment_day",
            "payment_method",
            "has_deduction_agreement",
            "has_raise",
            "raise_note",
            "has_bonus",
            "bonus_note",
            "has_retirement_allowance",
            "retirement_allowance_note",
            "leave_allowance_rate",
        ),
    ),
    (
        _("VIII. Resignation"),
        ("resignation_notice_days", "work_rules_articles_resignation"),
    ),
    (
        _("IX. Others"),
        (
            "insurances",
            "insurance_other",
            "hiring_health_check_on",
            "first_periodic_health_check_on",
            "periodic_health_check_interval",
            "treatment_contact_department",
            "treatment_contact_person",
            "treatment_contact_phone",
            "consultation_contact_department",
            "consultation_contact_person",
            "consultation_contact_phone",
            "work_rules_location",
        ),
    ),
    (_("Equal pay"), ("comparable_japanese_note", "comparable_japanese_wage")),
)

FIELDS = tuple(name for _title, names in SECTIONS for name in names)

# 条件つきの入力欄（forms.js。JS が無いときは services.contracts.conditional_errors で弾く）
ENABLED_WHEN: dict[str, dict[str, str]] = {
    "renewal_criteria": {"renewal": "possible"},
    "renewal_limit_count": {"has_renewal_limit": "on"},
    "renewal_limit_years": {"has_renewal_limit": "on"},
    "indefinite_conversion": {"ends_on": "*"},  # 期間の定めの無い契約には無い欄
    "conversion_conditions_change": {"indefinite_conversion": "on"},
    "early_paid_leave_months": {"has_early_paid_leave": "on"},
    "early_paid_leave_days": {"has_early_paid_leave": "on"},
    "raise_note": {"has_raise": "on"},
    "bonus_note": {"has_bonus": "on"},
    "retirement_allowance_note": {"has_retirement_allowance": "on"},
}


class SiteChoiceField(forms.ModelChoiceField):  # type: ignore[type-arg]
    """事業所の選択（受入れ機関ごとにまとめる。受入れ機関は事業所から決める。T28b の D6）。"""

    def label_from_instance(self, obj: Any) -> str:
        return f"{obj.name}（{obj.address}）"


def _grouped(queryset: Any) -> list[tuple[str, list[tuple[Any, str]]]]:
    groups: dict[str, list[tuple[Any, str]]] = {}
    for site in queryset:
        groups.setdefault(site.host_organization.name, []).append(
            (site.pk, f"{site.name}（{site.address}）")
        )
    return list(groups.items())


class VersionForm(StyledFormMixin, forms.ModelForm[EmploymentContractVersion]):
    """契約の登録（第1版）と新しい版。版は作った後に変えない。"""

    site = SiteChoiceField(label=_("host site"), queryset=HostSite.objects.none())
    categories = forms.ModelMultipleChoiceField(
        label=_("operation categories"),
        queryset=OperationCategory.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        help_text=_("Select from the categories registered for the site."),
    )

    class Meta:
        model = EmploymentContractVersion
        fields = FIELDS
        widgets = {
            name: forms.DateInput
            for name in (
                "changed_on",
                "signed_on",
                "starts_on",
                "ends_on",
                "expected_entry_on",
                "hiring_health_check_on",
                "first_periodic_health_check_on",
            )
        } | {
            "start_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "end_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "change_reason": forms.Textarea(attrs={"rows": 2}),
            "workplace_change_scope": forms.Textarea(attrs={"rows": 2}),
            "duties": forms.Textarea(attrs={"rows": 2}),
            "duty_change_scope": forms.Textarea(attrs={"rows": 2}),
            "temporary_return_leave_note": forms.Textarea(attrs={"rows": 2}),
            "comparable_japanese_note": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(
        self, *args: Any, contract: EmploymentContract | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self.contract = contract
        sites = HostSite.objects.filter(is_active=True, host_organization__is_active=True)
        if contract is not None:
            # 新しい版は契約の受入れ機関の事業所だけ（雇用主が変わるなら新しい契約。D6）
            current = self.initial.get("site")
            sites = HostSite.objects.filter(host_organization=contract.host_organization).filter(
                Q(is_active=True) | Q(pk=current)
            )
            # 2 版目以降は変更日・変更理由が必須
            self.fields["changed_on"].required = True
            self.fields["change_reason"].required = True
        else:
            del self.fields["change_reason"]
            self.fields["changed_on"].required = False
            self.fields["changed_on"].help_text = _(
                "Leave blank to use the start date (version 1)."
            )
        sites = sites.select_related("host_organization").order_by("host_organization__name", "pk")
        site_field = self.fields["site"]
        assert isinstance(site_field, forms.ModelChoiceField)
        site_field.queryset = sites
        site_field.choices = [("", "---------"), *_grouped(sites)]
        site_field.help_text = _(
            "The host organization (employer) is the one of the site. Dispatch is not allowed."
        )
        category_field = self.fields["categories"]
        assert isinstance(category_field, forms.ModelMultipleChoiceField)
        category_field.queryset = OperationCategory.objects.filter(sites__in=sites).distinct()
        self.fields["signed_on"].help_text = _(
            "Date of conclusion (version 1) or of the agreement to the change."
        )
        self.style_fields()
        for name, conditions in ENABLED_WHEN.items():
            if name in self.fields:
                self.enable_when(name, **conditions)

    def sections(self) -> list[tuple[Any, list[Any]]]:
        return [
            (title, [self[name] for name in names if name in self.fields])
            for title, names in SECTIONS
        ]


class ShiftForm(StyledFormMixin, forms.ModelForm[EmploymentContractShift]):
    class Meta:
        model = EmploymentContractShift
        fields = ("start_time", "end_time", "applies_to", "daily_working_minutes")
        widgets = {
            "start_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "end_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()


class AllowanceForm(StyledFormMixin, forms.ModelForm[EmploymentContractAllowance]):
    class Meta:
        model = EmploymentContractAllowance
        fields = ("name", "amount", "calculation_method", "fixed_overtime_hours")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()


class DeductionForm(StyledFormMixin, forms.ModelForm[EmploymentContractDeduction]):
    class Meta:
        model = EmploymentContractDeduction
        fields = ("kind", "name", "amount")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()


# 空の行は無視し、写した行は「削除」で外せる（明細は版ごとに作り直す）
ShiftFormSet = forms.formset_factory(ShiftForm, extra=0, can_delete=True)
AllowanceFormSet = forms.formset_factory(AllowanceForm, extra=0, can_delete=True)
DeductionFormSet = forms.formset_factory(DeductionForm, extra=0, can_delete=True)


def rows_of(formset: forms.BaseFormSet[Any]) -> list[Any]:
    """入力のある、削除にしていない行のモデル（保存前）。

    前の版から写した行は初期値のままでも保存する（has_changed で見ると落ちてしまう）。
    足したまま空の行は cleaned_data が空なので入れない。
    """
    return [
        form.save(commit=False)
        for form in formset.forms
        if form.cleaned_data and not form.cleaned_data.get("DELETE")
    ]


class TerminationForm(StyledFormMixin, forms.ModelForm[EmploymentContract]):
    class Meta:
        model = EmploymentContract
        fields = ("terminated_on", "termination_reason", "termination_note")
        widgets = {
            "terminated_on": forms.DateInput,
            "termination_note": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["terminated_on"].required = True
        self.fields["termination_reason"].required = True
        self.fields["termination_note"].help_text = _("Required when the reason is “other”.")
        self.style_fields()
        self.enable_when("termination_note", termination_reason=TerminationReason.OTHER)


class VersionAttachmentForm(StyledFormMixin, forms.Form):
    kind = forms.ChoiceField(
        label=_("kind"),
        choices=[(kind.value, kind.label) for kind in contracts.VERSION_ATTACHMENT_KINDS],
    )
    uploads = MultipleFileField(
        label=_("files"),
        help_text=_("PDF, PNG or JPEG, up to 10 MB each. You can select more than one."),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()


class YesNoSelect(forms.NullBooleanSelect):
    """有・無・未入力（未入力は「---------」）。"""

    def __init__(self, attrs: dict[str, Any] | None = None) -> None:
        super().__init__(attrs)
        self.choices = [
            ("unknown", "---------"),
            ("true", _("exists")),
            ("false", _("does not exist")),
        ]


class WageExplanationForm(StyledFormMixin, forms.ModelForm[ContractWageExplanation]):
    """報酬の説明（参考様式第1-4号の元データ。T34-D2）。版と違って直せる（監査ログに残る）。"""

    class Meta:
        model = ContractWageExplanation
        fields = (
            "comparison",
            "wage_unit",
            "applicant_duties",
            "applicant_experience_years",
            "applicant_other",
            "japanese_duties",
            "japanese_age",
            "japanese_sex",
            "japanese_experience_years",
            "japanese_wage",
            "has_wage_rules",
            "rules_wage",
            "reason",
            "other",
        )
        widgets = {
            name: forms.Textarea(attrs={"rows": 3})
            for name in (
                "applicant_duties",
                "applicant_other",
                "japanese_duties",
                "reason",
                "other",
            )
        } | {"has_wage_rules": YesNoSelect}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()
        self.enable_when("rules_wage", has_wage_rules="true")


class ContractGenerateForm(StyledFormMixin, forms.Form):
    """雇用契約の書類（第1-4号・第1-5号・第1-6号）の生成: 基準日・作成責任者（第1-4号）・警告。"""

    basis_on = forms.DateField(
        label=_("basis date (planned application date)"),
        widget=forms.DateInput,
        help_text=_("Used to choose the form version and for the checks."),
    )
    author = forms.CharField(
        label=_("person responsible for preparing the document (title and name)"),
        max_length=100,
        required=False,
        help_text=_("Printed at the bottom of form 1-4. The default is the representative."),
    )
    accept_warnings = forms.BooleanField(
        label=_("I have checked the warnings and will generate the form anyway."), required=False
    )

    def __init__(self, *args: Any, with_author: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if not with_author:
            del self.fields["author"]
        self.style_fields()
