"""受入れ機関の一括申請の項目（決算・申告の版・役員・申請担当者。T46b、ADR-0038）のフォーム。"""

from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.core.web_forms import StyledFormMixin
from apps.hosts.models import (
    HostApplicantContact,
    HostDeclaration,
    HostFinancialYear,
    HostOfficer,
)
from services import host_declarations


class FinancialYearForm(StyledFormMixin, forms.ModelForm[HostFinancialYear]):
    class Meta:
        model = HostFinancialYear
        fields = ("fiscal_year_end", "sales", "ordinary_profit", "net_profit", "net_assets")
        widgets = {"fiscal_year_end": forms.DateInput}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["sales"].help_text = _(
            "In yen. Used as the annual sales of the bulk application."
        )
        self.style_fields()


class OfficerForm(StyledFormMixin, forms.ModelForm[HostOfficer]):
    class Meta:
        model = HostOfficer
        fields = ("name", "name_kana", "position", "address", "appointed_on", "resigned_on")
        widgets = {"appointed_on": forms.DateInput, "resigned_on": forms.DateInput}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["resigned_on"].help_text = _(
            "Enter it when the officer resigns. The row stays."
        )
        self.style_fields()

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        appointed, resigned = cleaned.get("appointed_on"), cleaned.get("resigned_on")
        if appointed and resigned and resigned < appointed:
            self.add_error("resigned_on", _("The resignation date is before the appointment date."))
        return cleaned


class ApplicantContactForm(StyledFormMixin, forms.ModelForm[HostApplicantContact]):
    class Meta:
        model = HostApplicantContact
        fields = (
            "name",
            "relationship",
            "postal_code",
            "address",
            "phone_number",
            "mobile_number",
            "is_active",
        )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()


ANSWER_CHOICES = [("", _("not answered")), ("yes", _("yes")), ("no", _("no"))]


class DeclarationForm(StyledFormMixin, forms.ModelForm[HostDeclaration]):
    """申告の新しい版。項目ごとに有無と内容（200 字）。前の版の答えを初期値にする。"""

    class Meta:
        model = HostDeclaration
        fields = (
            "declared_on",
            "left_japanese_voluntary",
            "left_japanese_involuntary",
            "left_foreign_voluntary",
            "left_foreign_involuntary",
            "missing_ssw",
            "missing_interns",
            "missing_at_fault",
        )
        widgets = {"declared_on": forms.DateInput}

    def __init__(self, *args: Any, previous: HostDeclaration | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        answers = previous.answers if previous is not None else {}
        for item in host_declarations.items():
            answer = forms.ChoiceField(
                label=item.label,
                choices=ANSWER_CHOICES,
                required=False,
                widget=forms.RadioSelect,
            )
            self.fields[f"answer_{item.key}"] = answer
            initial = answers.get(item.key, {}).get("answer") or item.expected
            self.initial.setdefault(f"answer_{item.key}", initial)
            if item.detail:
                self.fields[f"detail_{item.key}"] = forms.CharField(
                    label=_("details of %(item)s") % {"item": f"({item.key})"},
                    max_length=200,
                    required=False,
                    widget=forms.Textarea(attrs={"rows": 2}),
                )
                self.initial.setdefault(
                    f"detail_{item.key}", answers.get(item.key, {}).get("detail", "")
                )
        if previous is not None:
            for name in self.Meta.fields[1:]:
                self.initial.setdefault(name, getattr(previous, name))
        self.style_fields()

    def answer_fields(self) -> list[tuple[Any, Any]]:
        """(有無の欄, 内容の欄か None) の並び（テンプレートで 1 項目ずつ出す）。"""
        return [
            (
                self[f"answer_{item.key}"],
                self[f"detail_{item.key}"] if item.detail else None,
            )
            for item in host_declarations.items()
        ]

    def count_fields(self) -> list[Any]:
        return [self[name] for name in self.Meta.fields[1:]]

    def answers(self) -> dict[str, dict[str, str]]:
        data = self.cleaned_data
        return {
            item.key: {
                "answer": data.get(f"answer_{item.key}") or "",
                "detail": (data.get(f"detail_{item.key}") or "").strip(),
            }
            for item in host_declarations.items()
        }

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        for item in host_declarations.items():
            answer = cleaned.get(f"answer_{item.key}")
            # 内容の欄は「有」のときに要る（一括登録用ツールの条件。(29) は「有」が通常の答え）
            if item.detail and answer == "yes":
                if not (cleaned.get(f"detail_{item.key}") or "").strip():
                    self.add_error(
                        f"detail_{item.key}",
                        _("Enter the details when the answer is yes."),
                    )
        return cleaned
