from typing import Any

from django import forms
from django.contrib import admin
from django.forms import BaseInlineFormSet
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _

from apps.core.admin import TRACKED_READONLY_FIELDS, AttachmentInline, TrackedModelAdmin
from apps.core.forms import allow_formatted_number_input
from apps.core.models import AttachmentKind
from services import corporate_number, hosts

from .models import (
    HostCooperationConfirmation,
    HostMembership,
    HostOrganization,
    HostSite,
    OperationCategory,
    SpecifiedSkillField,
    SupportContract,
)


class NoDeleteAdmin(TrackedModelAdmin[Any]):
    """受入れ機関側の記録は削除しない（無効化・期間で表す）。"""

    readonly_fields = TRACKED_READONLY_FIELDS

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


# --- マスタ ----------------------------------------------------------------------


class OperationCategoryInline(admin.TabularInline[OperationCategory, SpecifiedSkillField]):
    model = OperationCategory
    extra = 0
    can_delete = False
    fields = ("code", "name", "valid_from", "valid_until", "sort_order")


@admin.register(SpecifiedSkillField)
class SpecifiedSkillFieldAdmin(NoDeleteAdmin):
    list_display = ("name", "competent_ministry", "membership_body_name", "is_active")
    inlines = [OperationCategoryInline]


@admin.register(OperationCategory)
class OperationCategoryAdmin(NoDeleteAdmin):
    list_display = ("name", "field", "valid_from", "valid_until", "sort_order")
    list_filter = ("field",)


# --- 受入れ機関 --------------------------------------------------------------------


class HostOrganizationForm(forms.ModelForm[HostOrganization]):
    class Meta:
        model = HostOrganization
        # users（将来の受入れ機関ポータル用）は出さない
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
            "is_active",
        )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        allow_formatted_number_input(self.fields["corporate_number"])

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
            except forms.ValidationError as error:
                for field_name, messages in error.message_dict.items():
                    self.add_error(field_name, messages)
        return cleaned


class HostMembershipInline(admin.TabularInline[HostMembership, HostOrganization]):
    model = HostMembership
    extra = 0
    can_delete = False
    fields = ("field", "joined_on", "valid_until", "member_number")
    show_change_link = True  # 証明書の添付は加入状況の画面で行う


class CooperationConfirmationForm(forms.ModelForm[HostCooperationConfirmation]):
    class Meta:
        model = HostCooperationConfirmation
        fields = ("host_organization", "site", "municipality", "submitted_on")

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        host = cleaned.get("host_organization") or getattr(self.instance, "host_organization", None)
        try:
            hosts.validate_cooperation_site(getattr(host, "pk", None), cleaned.get("site"))
        except forms.ValidationError as error:
            self.add_error(None, error)
        return cleaned


class HostCooperationConfirmationInline(
    admin.TabularInline[HostCooperationConfirmation, HostOrganization]
):
    model = HostCooperationConfirmation
    form = CooperationConfirmationForm
    extra = 0
    can_delete = False
    fields = ("site", "municipality", "submitted_on")
    show_change_link = True  # 協力確認書の添付は個別の画面で行う

    def formfield_for_foreignkey(self, db_field: Any, request: HttpRequest, **kwargs: Any) -> Any:
        # 事業所はこの受入れ機関のものだけ（新規の受入れ機関では選べないので、事業所を先に登録する）
        if db_field.name == "site":
            match = request.resolver_match
            host_id = match.kwargs.get("object_id") if match else None
            kwargs["queryset"] = HostSite.objects.filter(host_organization_id=host_id)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


class SupportContractForm(forms.ModelForm[SupportContract]):
    class Meta:
        model = SupportContract
        fields = ("host_organization", "concluded_on", "starts_on", "ends_on", "scope")

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        host = cleaned.get("host_organization") or getattr(self.instance, "host_organization", None)
        try:
            hosts.validate_contract_period(
                getattr(host, "pk", None),
                cleaned.get("starts_on"),
                cleaned.get("ends_on"),
                exclude_pk=self.instance.pk,
            )
        except forms.ValidationError as error:
            self.add_error("starts_on", error)
        return cleaned


class SupportContractFormSet(BaseInlineFormSet):  # type: ignore[type-arg]
    """同じ画面で入力した契約どうしの重なりも拒否する（新規登録時は DB に比べる相手がないため）。"""

    def clean(self) -> None:
        super().clean()
        periods: list[tuple[Any, Any]] = []
        for form in self.forms:
            data = getattr(form, "cleaned_data", None) or {}  # 空の追加行は {}
            if data.get("starts_on") and not data.get("DELETE"):
                periods.append((data["starts_on"], data.get("ends_on")))
        for i, a in enumerate(periods):
            if any(hosts.periods_overlap(a, b) for b in periods[i + 1 :]):
                raise forms.ValidationError(
                    _("Support contracts entered on this page overlap each other.")
                )


class SupportContractInline(admin.TabularInline[SupportContract, HostOrganization]):
    model = SupportContract
    form = SupportContractForm
    formset = SupportContractFormSet
    extra = 0
    can_delete = False
    fields = ("concluded_on", "starts_on", "ends_on", "scope")
    show_change_link = True  # 契約書の添付は契約の画面で行う


class HostSiteInline(admin.TabularInline[HostSite, HostOrganization]):
    """事業所の一覧（登録・編集は事業所の画面で行う。業務区分の選択があるため）。"""

    model = HostSite
    extra = 0
    can_delete = False
    fields = ("name", "field", "industry_code", "address", "is_active")
    readonly_fields = fields
    show_change_link = True

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(HostOrganization)
class HostOrganizationAdmin(NoDeleteAdmin):
    form = HostOrganizationForm
    list_display = ("name", "entity_type", "corporate_number", "representative_name", "is_active")
    list_filter = ("entity_type", "sites__field", "memberships__field", "is_active")
    search_fields = ("name", "name_kana", "corporate_number", "representative_name")
    fieldsets = (
        (None, {"fields": ("entity_type", "name", "name_kana", "corporate_number")}),
        (_("Representative"), {"fields": ("representative_title", "representative_name")}),
        (_("Contact"), {"fields": ("postal_code", "address", "phone_number", "is_active")}),
        (_("Record"), {"fields": TRACKED_READONLY_FIELDS}),
    )
    inlines = [
        HostMembershipInline,
        HostCooperationConfirmationInline,
        SupportContractInline,
        HostSiteInline,
    ]


class MembershipAttachmentInline(AttachmentInline):
    kinds = (AttachmentKind.MEMBERSHIP_CERTIFICATE,)


@admin.register(HostMembership)
class HostMembershipAdmin(NoDeleteAdmin):
    list_display = ("host_organization", "field", "joined_on", "valid_until")
    list_filter = ("field",)
    inlines = [MembershipAttachmentInline]


class CooperationAttachmentInline(AttachmentInline):
    kinds = (AttachmentKind.COOPERATION_CONFIRMATION,)


@admin.register(HostCooperationConfirmation)
class HostCooperationConfirmationAdmin(NoDeleteAdmin):
    form = CooperationConfirmationForm
    list_display = ("host_organization", "site", "municipality", "submitted_on")
    search_fields = ("host_organization__name", "municipality")
    inlines = [CooperationAttachmentInline]


class ContractAttachmentInline(AttachmentInline):
    kinds = (AttachmentKind.SUPPORT_CONTRACT,)


@admin.register(SupportContract)
class SupportContractAdmin(NoDeleteAdmin):
    form = SupportContractForm
    list_display = ("host_organization", "concluded_on", "starts_on", "ends_on", "scope")
    inlines = [ContractAttachmentInline]


class HostSiteForm(forms.ModelForm[HostSite]):
    class Meta:
        model = HostSite
        fields = (
            "host_organization",
            "name",
            "postal_code",
            "address",
            "industry_code",
            "field",
            "categories",
            "is_active",
        )

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        try:
            hosts.validate_site_categories(cleaned.get("field"), cleaned.get("categories") or [])
        except forms.ValidationError as error:
            self.add_error("categories", error)
        return cleaned


@admin.register(HostSite)
class HostSiteAdmin(NoDeleteAdmin):
    form = HostSiteForm
    list_display = ("name", "host_organization", "field", "industry_code", "is_active")
    list_filter = ("field", "categories", "is_active")
    search_fields = ("name", "host_organization__name", "address")
    filter_horizontal = ("categories",)
