"""受入れ機関の業務画面のフォーム（T28a）。検査の中身は services.hosts・services.host_records。"""

from __future__ import annotations

from typing import Any

from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.forms import allow_formatted_number_input
from apps.core.models import AttachmentKind
from apps.core.web_forms import FilteredCheckboxSelectMultiple, StyledFormMixin
from apps.hosts.models import (
    FeeTiming,
    HostCooperationConfirmation,
    HostMembership,
    HostOrganization,
    HostSite,
    OperationCategory,
    SpecifiedSkillField,
    SupportContract,
    SupportTerminationCategory,
    SupportTerminationReason,
)
from apps.support.record_forms import MultipleFileField
from services import attachments, corporate_number, hosts

FILES_HELP = _("PDF, PNG or JPEG, up to 10 MB each. You can select more than one.")


def apply_errors(form: forms.BaseForm, error: ValidationError) -> None:
    """サービスの検査の結果を欄に付ける（欄の無いキーはフォーム全体のエラー）。"""
    if not hasattr(error, "error_dict"):
        form.add_error(None, error)
        return
    for key, messages in error.error_dict.items():
        form.add_error(key if key in form.fields else None, messages)


class HostOrganizationForm(StyledFormMixin, forms.ModelForm[HostOrganization]):
    class Meta:
        model = HostOrganization
        # users（将来の受入れ機関ポータル用）・is_active（効いている所が無い）は出さない
        fields = (
            "entity_type",
            "name",
            "name_kana",
            "corporate_number",
            "representative_title",
            "representative_name",
            "postal_code",
            "address",
            "phone_number",
        )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        allow_formatted_number_input(self.fields["corporate_number"])
        self.fields["name_kana"].help_text = _("Used for the furigana of the support plan.")
        self.fields["address"].help_text = _("Head office or principal office.")
        self.style_fields()
        # 個人事業主を選ぶと入力できなくする（JS が無いときは保存時の検査で弾く）
        self.enable_when("corporate_number", entity_type="corporation")

    def clean_corporate_number(self) -> str:
        value = corporate_number.normalize(self.cleaned_data.get("corporate_number") or "")
        corporate_number.validate(value)
        return value

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        if "corporate_number" in cleaned and cleaned.get("entity_type"):
            try:
                hosts.validate_corporate_number_for(
                    cleaned["entity_type"], cleaned["corporate_number"]
                )
            except ValidationError as error:
                apply_errors(self, error)
        return cleaned


def _category_label(category: OperationCategory) -> str:
    period = ""
    if category.valid_from or category.valid_until:
        start = category.valid_from.isoformat() if category.valid_from else ""
        end = category.valid_until.isoformat() if category.valid_until else ""
        period = f"（{start}〜{end}）"
    return f"{category.field.name}: {category.name}{period}"


class HostSiteForm(StyledFormMixin, forms.ModelForm[HostSite]):
    categories = forms.ModelMultipleChoiceField(
        label=_("operation categories"),
        queryset=OperationCategory.objects.none(),
        widget=FilteredCheckboxSelectMultiple(source="id_field"),
        help_text=_("Select the categories of the site's field. More than one can be selected."),
    )

    class Meta:
        model = HostSite
        fields = (
            "name",
            "postal_code",
            "address",
            "phone_number",
            "industry_code",
            "field",
            "is_active",
        )

    field_order = (
        "name",
        "postal_code",
        "address",
        "phone_number",
        "industry_code",
        "field",
        "categories",
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        today = timezone.localdate()
        selected = list(self.instance.categories.all()) if self.instance.pk else []
        # 今日有効な区分と、今後始まる区分（2027-04-01 の切替）。いま付いている区分は残す
        self.fields["categories"].queryset = (  # type: ignore[attr-defined]
            OperationCategory.objects.filter(
                Q(valid_until__isnull=True)
                | Q(valid_until__gte=today)
                | Q(pk__in=[c.pk for c in selected])
            )
            .select_related("field")
            .order_by("field__sort_order", "sort_order", "code")
        )
        self.fields["categories"].label_from_instance = _category_label  # type: ignore[attr-defined]
        # 分野を選ぶと、その分野の区分だけを見せる（forms.js。修正 1 回目 B1）
        widget = self.fields["categories"].widget
        assert isinstance(widget, FilteredCheckboxSelectMultiple)
        widget.filter_values = {
            str(category.pk): [str(category.field_id)]
            for category in self.fields["categories"].queryset  # type: ignore[attr-defined]
        }
        if selected:
            self.initial.setdefault("categories", selected)
        field_queryset = SpecifiedSkillField.objects.filter(is_active=True)
        if self.instance.pk:
            field_queryset = SpecifiedSkillField.objects.filter(
                Q(is_active=True) | Q(pk=self.instance.field_id)
            )
        else:
            del self.fields["is_active"]  # 新しい事業所は有効
        self.fields["field"].queryset = field_queryset  # type: ignore[attr-defined]
        self.fields["phone_number"].help_text = _(
            "The contact of the workplace in the employment conditions (form 1-6, II)."
        )
        self.fields["industry_code"].help_text = _(
            "Japan Standard Industrial Classification. 2 to 4 digits (e.g. 09, 0991, 2194)."
        )
        self.style_fields()

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        try:
            hosts.validate_site_categories(cleaned.get("field"), cleaned.get("categories") or [])
        except ValidationError as error:
            self.add_error("categories", error)
        return cleaned


class MembershipForm(StyledFormMixin, forms.ModelForm[HostMembership]):
    """加入状況の登録・編集。分野は登録した後は変えない。証明書は登録のときに 1 つ付けられる。"""

    certificate = forms.FileField(
        label=_("membership certificate"),
        required=False,
        help_text=_(
            "Council: certificate of membership. Registered corporation: printout of the member "
            "list showing the organization. PDF, PNG or JPEG, up to 10 MB."
        ),
    )

    class Meta:
        model = HostMembership
        fields = ("field", "joined_on", "valid_until", "member_number")
        widgets = {"joined_on": forms.DateInput, "valid_until": forms.DateInput}

    def __init__(self, *args: Any, host: HostOrganization, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.instance.host_organization = host
        if self.instance.pk:
            del self.fields["field"]
            del self.fields["certificate"]  # 差し替えは証明書の画面で行う
        else:
            taken = host.memberships.values_list("field_id", flat=True)
            self.fields["field"].queryset = SpecifiedSkillField.objects.filter(  # type: ignore[attr-defined]
                is_active=True
            ).exclude(pk__in=list(taken))
            self.fields["field"].label_from_instance = (  # type: ignore[attr-defined]
                lambda field: f"{field.name}（{field.membership_body_name}）"
            )
        self.fields["valid_until"].help_text = _(
            "Leave blank if the certificate has no expiry date."
        )
        self.style_fields()

    def clean_certificate(self) -> Any:
        file = self.cleaned_data.get("certificate")
        if file:
            attachments.validate_upload(AttachmentKind.MEMBERSHIP_CERTIFICATE, file)
        return file


class CertificateForm(StyledFormMixin, forms.Form):
    """加入証明の差し替え（古い証明書は残る）。"""

    file = forms.FileField(label=_("membership certificate"))
    valid_until = forms.DateField(
        label=_("valid until"),
        required=False,
        widget=forms.DateInput,
        help_text=_(
            "The expiry date of the new certificate. Leave blank if it has none. "
            "The expiry date of the membership is updated to this date."
        ),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()

    def clean_file(self) -> Any:
        file = self.cleaned_data["file"]
        attachments.validate_upload(AttachmentKind.MEMBERSHIP_CERTIFICATE, file)
        return file


class CooperationForm(StyledFormMixin, forms.ModelForm[HostCooperationConfirmation]):
    uploads = MultipleFileField(
        label=_("copy of the confirmation"), required=False, help_text=FILES_HELP
    )

    class Meta:
        model = HostCooperationConfirmation
        fields = ("site", "municipality", "submitted_on")
        widgets = {"submitted_on": forms.DateInput}

    def __init__(self, *args: Any, host: HostOrganization, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.instance.host_organization = host
        sites = host.sites.filter(is_active=True)
        if self.instance.pk:
            sites = host.sites.filter(Q(is_active=True) | Q(pk=self.instance.site_id))
            del self.fields["uploads"]  # 控えの追加は添付の画面で行う
        self.fields["site"].queryset = sites.order_by("pk")  # type: ignore[attr-defined]
        self.fields["site"].label_from_instance = (  # type: ignore[attr-defined]
            lambda site: f"{site.name}（{site.address}）"
        )
        self.fields["site"].help_text = _(
            "The site written in the confirmation. Submissions to the municipality of the "
            "worker's residence are also recorded under the site."
        )
        self.fields["municipality"].help_text = _(
            "The municipality of the site or of the worker's residence (e.g. Yokohama City). "
            "When you submit it again, register a new submission. The previous one is kept."
        )
        self.style_fields()

    def clean_submitted_on(self) -> Any:
        value = self.cleaned_data["submitted_on"]
        if value and value > timezone.localdate():
            raise ValidationError(_("The date cannot be in the future."), code="future")
        return value


class SupportContractForm(StyledFormMixin, forms.ModelForm[SupportContract]):
    uploads = MultipleFileField(
        label=_("support entrustment contract"), required=False, help_text=FILES_HELP
    )
    # 既存の行の委託料・期間を直すとき: 契約の変更（第3-3-1号を起票）か、誤りの訂正か（ADR-0017）
    is_amendment = forms.BooleanField(
        label=_("this is a change of the contract (not a correction of a mistake)"),
        required=False,
        help_text=_(
            "When the fee or the period changes, notification tasks (form 3-3-1) are created "
            "for each worker. Leave unchecked to correct a mistake."
        ),
    )
    amended_on = forms.DateField(
        label=_("date of the change"), required=False, widget=forms.DateInput
    )

    class Meta:
        model = SupportContract
        fields = ("concluded_on", "starts_on", "ends_on", "monthly_fee")
        widgets = {
            "concluded_on": forms.DateInput,
            "starts_on": forms.DateInput,
            "ends_on": forms.DateInput,
        }

    def __init__(self, *args: Any, host: HostOrganization, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.instance.host_organization = host
        if self.instance.pk:
            del self.fields["uploads"]  # 契約書の追加は添付の画面で行う
        else:
            del self.fields["is_amendment"]
            del self.fields["amended_on"]
        self.fields["ends_on"].help_text = _(
            "The planned end date. One row is one period: when the contract is renewed "
            "automatically, register the renewed period as the next contract (it starts the day "
            "after this end date). Register an actual termination with “Register the "
            "termination”. Leave blank if there is no end date."
        )
        self.fields["monthly_fee"].help_text = _(
            "Form 1-25, item 5 and form 5-10, article 2. Enter the breakdown (annex of form "
            "5-10) as the fee breakdown."
        )
        self.style_fields()
        if "amended_on" in self.fields:
            self.enable_when("amended_on", is_amendment="on")

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        try:
            hosts.validate_contract_period(
                self.instance.host_organization_id,
                cleaned.get("starts_on"),
                cleaned.get("ends_on"),
                exclude_pk=self.instance.pk,
                terminated_on=self.instance.terminated_on,
            )
        except ValidationError as error:
            self.add_error("starts_on", error)
        if cleaned.get("is_amendment") and not cleaned.get("amended_on"):
            self.add_error("amended_on", _("Enter the date of the change."))
        return cleaned


class SupportTerminationForm(StyledFormMixin, forms.Form):
    """支援委託契約の終了の登録（第3-3-2号 A。予定の終了日は残す）。"""

    terminated_on = forms.DateField(
        label=_("terminated on"),
        widget=forms.DateInput,
        help_text=_("The actual end date. It must be within the contract period."),
    )
    termination_category = forms.ChoiceField(
        label=_("termination category"),
        choices=[("", "---------"), *SupportTerminationCategory.choices],
    )
    termination_reason = forms.ChoiceField(
        label=_("termination reason"),
        choices=[("", "---------"), *SupportTerminationReason.choices],
        help_text=_(
            "Expiry: expiry of the period. Host organization: business reasons, breach of "
            "contract or other. Support organization: revocation of registration, breach of "
            "contract, business reasons or other."
        ),
    )
    termination_note = forms.CharField(
        label=_("termination reason (other)"),
        required=False,
        max_length=20,
        help_text=_("Up to 20 characters (written on form 3-3-2)."),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()
        self.enable_when("termination_note", termination_reason="other")


class SupportFeeForm(StyledFormMixin, forms.Form):
    """委託料の内訳の 1 行（第5-10号 別紙。T37-D9）。"""

    name = forms.CharField(label=_("item"), max_length=100)
    amount = forms.IntegerField(label=_("amount (yen)"), min_value=0)
    timing = forms.ChoiceField(label=_("when collected"), choices=FeeTiming.choices)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()


SupportFeeFormSet = forms.formset_factory(SupportFeeForm, extra=0, can_delete=True)
