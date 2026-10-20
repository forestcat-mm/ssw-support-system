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
    language = forms.ChoiceField(
        label=_("language of the form"),
        required=False,
        help_text=_("The Chinese translation is self-made and generated as a separate document."),
    )

    def __init__(
        self, *args: Any, languages: list[tuple[str, str]] | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        if languages and len(languages) > 1:  # 訳文の版がある様式だけ（T43）
            self.fields["language"].choices = languages  # type: ignore[attr-defined]
        else:
            del self.fields["language"]
        self.style_fields()

    @property
    def chosen_language(self) -> str:
        if "language" not in self.fields or not self.is_bound or not self.is_valid():
            return str(self.initial.get("language") or "ja")
        return str(self.cleaned_data.get("language") or "ja")


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


class PeriodicReportForm(StyledFormMixin, forms.Form):
    """定期届出の年度（届出対象期間 4月1日〜翌年3月31日。T51）。既定は前の年度。"""

    fiscal_year = forms.IntegerField(
        label=_("fiscal year"),
        min_value=2019,
        max_value=2100,
        help_text=_("The period from April 1 of this year to March 31 of the next year."),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()


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


class BulkGenerateForm(StyledFormMixin, forms.Form):
    """不足の書類の一括生成（T42）: 生成する行・第1-17号のⅢ欄７の申告・警告を確かめた印。

    基準日・申請の種類は一覧の条件をそのまま送る（隠しの欄）。
    """

    set_id = forms.CharField(widget=forms.HiddenInput)
    basis_on = forms.DateField(widget=forms.HiddenInput)
    rows = forms.MultipleChoiceField(
        label=_("documents to generate"), required=False, widget=forms.CheckboxSelectMultiple
    )
    propriety = PlanGenerateForm.base_fields["propriety"]
    accept_warnings = forms.BooleanField(
        label=_("I have checked the warnings and will generate the documents anyway."),
        required=False,
    )

    def __init__(
        self, *args: Any, row_choices: list[tuple[str, str]] | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self.fields["rows"].choices = row_choices or []  # type: ignore[attr-defined]
        self.style_fields()


class SubmissionBundleForm(StyledFormMixin, forms.Form):
    """提出用の統合 PDF（T42）: システムの外の行のアップロード・警告を確かめた印。

    アップロードの欄は行ごとに upload-<行の ID>（PDF・PNG・JPEG）。
    """

    set_id = forms.CharField(widget=forms.HiddenInput)
    basis_on = forms.DateField(widget=forms.HiddenInput)
    accept_warnings = forms.BooleanField(
        label=_("I have checked the warnings and will make the PDF anyway."), required=False
    )
    make_list = forms.BooleanField(
        label=_("Make the list of documents to submit (cover and tables 1 to 3) and put it first"),
        required=False,
        initial=True,
        help_text=_(
            "Written into the list distributed by the Immigration Services Agency. The mark of "
            "each row (yes / no) can be changed below."
        ),
    )

    MARKS = [("yes", _("yes (submitted)")), ("no", _("no")), ("", _("no mark"))]

    def __init__(
        self,
        *args: Any,
        upload_rows: list[str] | None = None,
        mark_rows: list[str] | None = None,
        saved_files: list[int] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        for attachment_id in saved_files or []:
            self.fields[f"remove-{attachment_id}"] = forms.BooleanField(
                label=_("remove this file"), required=False
            )
        for row_id in upload_rows or []:
            self.fields[f"upload-{row_id}"] = MultipleFileField(
                label=_("file to include"), required=False
            )
        for row_id in mark_rows or []:
            self.fields[f"mark-{row_id}"] = forms.ChoiceField(
                label=_("submission check"), choices=self.MARKS, required=False
            )
        self.style_fields()

    def marks(self) -> dict[str, str]:
        prefix = "mark-"
        return {
            name.removeprefix(prefix): str(value or "")
            for name, value in self.cleaned_data.items()
            if name.startswith(prefix)
        }

    def removed(self) -> list[int]:
        prefix = "remove-"
        return [
            int(name.removeprefix(prefix))
            for name, value in self.cleaned_data.items()
            if name.startswith(prefix) and value
        ]

    def remove_field(self, attachment_id: int) -> Any:
        name = f"remove-{attachment_id}"
        return self[name] if name in self.fields else None

    def mark_field(self, row_id: str) -> Any:
        name = f"mark-{row_id}"
        return self[name] if name in self.fields else None

    def uploads(self) -> dict[str, list[Any]]:
        prefix = "upload-"
        return {
            name.removeprefix(prefix): files
            for name, files in self.cleaned_data.items()
            if name.startswith(prefix) and files
        }

    def upload_field(self, row_id: str) -> Any:
        name = f"upload-{row_id}"
        return self[name] if name in self.fields else None


class PublicDutyPledgeForm(StyledFormMixin, forms.Form):
    """第1-26号 公的義務履行に関する誓約書の生成（作成日・理由・警告。T42 の追加）。"""

    RELIEF = "relief"
    OTHER = "other"

    basis_on = forms.DateField(
        label=_("date of preparation (basis date)"),
        widget=forms.DateInput,
        help_text=_("Used to choose the form version and printed as the date."),
    )
    reason = forms.ChoiceField(
        label=_("reason the obligations cannot be fulfilled promptly"),
        choices=[
            (RELIEF, _("relief measures (or exemption from social insurance) are applied for")),
            (OTHER, _("another unavoidable reason")),
        ],
        widget=forms.RadioSelect,
        required=False,
    )
    relief_on = forms.DateField(
        label=_("date of the application for relief measures"),
        widget=forms.DateInput,
        required=False,
    )
    other_reason = forms.CharField(
        label=_("specific reason"),
        widget=forms.Textarea(attrs={"rows": 3}),
        max_length=300,
        required=False,
    )
    accept_warnings = forms.BooleanField(
        label=_("I have checked the warnings and will generate the form anyway."), required=False
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()
        self.enable_when("relief_on", reason=self.RELIEF)
        self.enable_when("other_reason", reason=self.OTHER)

    def data_values(self) -> tuple[Any, str]:
        """(申請した日, 理由の文)。選ばなかった側は使わない。"""
        reason = self.cleaned_data.get("reason")
        relief_on = self.cleaned_data.get("relief_on") if reason == self.RELIEF else None
        other = (self.cleaned_data.get("other_reason") or "") if reason == self.OTHER else ""
        return relief_on, other
