"""帳票の生成の画面の入力（T31、ADR-0019・0020）。"""

from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.core.web_forms import StyledFormMixin
from apps.support.record_forms import MultipleFileField

NOT_LOADED = _("The form is not loaded. Ask an administrator to load the forms (make forms).")


class BriefingGenerateForm(StyledFormMixin, forms.Form):
    """確認書（第5-9号・第5-8号）の生成: 様式の言語と警告を確かめた印。

    言語は日中併記の版があるときだけ選ぶ（T38）。基準日は記録の実施日。
    """

    language = forms.ChoiceField(
        label=_("language of the form"),
        required=False,
        help_text=_(
            "The bilingual version is the one distributed by the Immigration Services Agency."
        ),
    )
    accept_warnings = forms.BooleanField(
        label=_("I have checked the warnings and will generate the form anyway."), required=False
    )

    def __init__(
        self, *args: Any, languages: list[tuple[str, str]] | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        if languages and len(languages) > 1:
            self.fields["language"].choices = languages  # type: ignore[attr-defined]
        else:
            del self.fields["language"]
        self.style_fields()

    @property
    def chosen_language(self) -> str:
        if "language" not in self.fields or not self.is_bound or not self.is_valid():
            return str(self.initial.get("language") or "ja")
        return str(self.cleaned_data.get("language") or "ja")


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


class SignatureForm(StyledFormMixin, forms.Form):
    """署名済みのスキャンと署名日（T33）。"""

    signed_on = forms.DateField(label=_("date of signature"), widget=forms.DateInput)
    scans = MultipleFileField(
        label=_("signed scan"),
        help_text=_("PDF, PNG or JPEG, up to 10 MB each."),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()


class ConsultationRecordForm(StyledFormMixin, forms.Form):
    """相談記録書（第5-4号）の期間（受入れ機関ごと。既定は今日を含む年度。T35-D9）。"""

    starts_on = forms.DateField(label=_("period from"), widget=forms.DateInput)
    ends_on = forms.DateField(label=_("period to"), widget=forms.DateInput)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        starts_on, ends_on = data.get("starts_on"), data.get("ends_on")
        if starts_on and ends_on and starts_on > ends_on:
            self.add_error("ends_on", _("The end of the period must be on or after the start."))
        return data


class ApplicationSetForm(StyledFormMixin, forms.Form):
    """申請の書類の一覧の条件（申請の種類・基準日。T37-D3）。"""

    set_id = forms.ChoiceField(
        label=_("application"),
        choices=[
            ("coe", _("Certificate of Eligibility")),
            ("chg", _("Change of status of residence")),
            ("ext", _("Extension of period of stay")),
        ],
    )
    basis_on = forms.DateField(
        label=_("basis date (planned application date)"), widget=forms.DateInput
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()
