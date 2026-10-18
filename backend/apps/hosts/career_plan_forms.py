"""キャリアアッププランの入力（T36、ADR-0025）。検査の中身は services.career_plans。"""

from __future__ import annotations

from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.core.web_forms import StyledFormMixin
from apps.hosts.models import HostOrganization, HostSite, SpecifiedSkillField
from services import career_plans


class CareerPlanForm(StyledFormMixin, forms.Form):
    field = forms.ModelChoiceField(
        label=_("specified skill field"), queryset=SpecifiedSkillField.objects.none()
    )
    site = forms.ModelChoiceField(
        label=_("site (only when it differs by site)"),
        queryset=HostSite.objects.none(),
        required=False,
        help_text=_("Leave blank to use the plan for all sites of the field."),
    )
    applies_from = forms.DateField(label=_("applies from"), widget=forms.DateInput)
    change_reason = forms.CharField(
        label=_("change reason"), widget=forms.Textarea(attrs={"rows": 2}), required=False
    )
    career_route = forms.CharField(
        label=_("expected career route"),
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("Example: worker → line leader → foreman (specified skilled worker (ii))."),
    )
    transfer_scope = forms.CharField(
        label=_("scope of job changes"),
        widget=forms.Textarea(attrs={"rows": 2}),
        required=False,
        help_text=_(
            "For the seafood processing category (from 1 April 2027): the scope within which "
            "the worker can change jobs (in principle the same category), explained for consent."
        ),
    )

    def __init__(self, *args: Any, host: HostOrganization, new_plan: bool, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if new_plan:
            del self.fields["change_reason"]
            field_ids = host.sites.values_list("field_id", flat=True)
            self.fields["field"].queryset = SpecifiedSkillField.objects.filter(  # type: ignore[attr-defined]
                pk__in=field_ids
            )
            self.fields["site"].queryset = host.sites.filter(is_active=True)  # type: ignore[attr-defined]
        else:
            del self.fields["field"]
            del self.fields["site"]
            self.fields["change_reason"].required = True
        self.style_fields()


class LevelForm(StyledFormMixin, forms.Form):
    name = forms.CharField(label=_("level"), max_length=50, help_text=_("Example: level 1."))
    years = forms.CharField(
        label=_("years as a guide"), max_length=50, help_text=_("Example: 1st to 2nd year.")
    )
    duties = forms.CharField(label=_("duties"), widget=forms.Textarea(attrs={"rows": 2}))
    requirements = forms.CharField(
        label=_("experience, results, qualifications and tests"),
        widget=forms.Textarea(attrs={"rows": 2}),
        required=False,
    )
    position = forms.CharField(label=_("position"), max_length=100, required=False)
    treatment = forms.CharField(
        label=_("treatment such as wages"),
        widget=forms.Textarea(attrs={"rows": 2}),
        required=False,
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()


LevelFormSet = forms.formset_factory(LevelForm, extra=0, can_delete=True)


def levels_of(formset: forms.BaseFormSet[Any]) -> list[career_plans.LevelInput]:
    return [
        career_plans.LevelInput(
            name=form.cleaned_data["name"],
            years=form.cleaned_data["years"],
            duties=form.cleaned_data["duties"],
            requirements=form.cleaned_data.get("requirements") or "",
            position=form.cleaned_data.get("position") or "",
            treatment=form.cleaned_data.get("treatment") or "",
        )
        for form in formset.forms
        if form.cleaned_data and not form.cleaned_data.get("DELETE")
    ]
