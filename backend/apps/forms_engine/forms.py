"""帳票の生成の画面の入力（T31、ADR-0019・0020）。"""

from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.core.web_forms import StyledFormMixin

NOT_LOADED = _("The form is not loaded. Ask an administrator to load the forms (make forms).")


class BriefingGenerateForm(StyledFormMixin, forms.Form):
    """確認書（第5-9号・第5-8号）の生成: 警告を確かめた印だけ（基準日は記録の実施日）。"""

    accept_warnings = forms.BooleanField(
        label=_("I have checked the warnings and will generate the form anyway."), required=False
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()


class PlanGenerateForm(StyledFormMixin, forms.Form):
    """支援計画書（第1-17号）の生成: 基準日・Ⅲ欄７の申告・警告を確かめた印。"""

    basis_on = forms.DateField(
        label=_("basis date (planned application date)"),
        widget=forms.DateInput,
        help_text=_("Used to choose the form version and for the checks."),
    )
    propriety = forms.BooleanField(
        label=_(
            "We declare that the support is ensured to be proper (section III-7: the support "
            "manager is not a relative or close associate of the host's officers, and neither "
            "the manager nor the staff fall under the grounds for refusing registration)."
        ),
        required=False,
    )
    accept_warnings = forms.BooleanField(
        label=_("I have checked the warnings and will generate the form anyway."), required=False
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()
