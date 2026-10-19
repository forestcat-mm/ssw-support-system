"""管理者向けの「設定」画面のフォーム（T15b）。業務のルールは services に置く。"""

import datetime
from typing import Any

from django import forms
from django.contrib.contenttypes.models import ContentType
from django.utils.translation import gettext_lazy as _

from apps.core.forms import InvitationForm
from apps.core.models import AttachmentKind, AuditAction, Office, Organization, Staff, User
from apps.core.web_forms import StyledFormMixin
from services import attachments, audit, organization
from services.audit_search import AuditQuery


class InviteForm(StyledFormMixin, InvitationForm):
    """ユーザーの招待（パスワードは本人が招待メールのリンクから設定する）。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()


class StaffForm(StyledFormMixin, forms.ModelForm[Staff]):
    class Meta:
        model = Staff
        fields = (
            "name",
            "name_kana",
            "position",
            "is_support_manager",
            "is_support_staff",
            "employment_type",
            "office",
            "languages",
            "training_completed_on",
            "has_life_consultation_experience",
            "life_consultation_experience_note",
            "user",
            "is_active",
        )
        widgets = {
            "languages": forms.CheckboxSelectMultiple,
            "training_completed_on": forms.DateInput,
            "life_consultation_experience_note": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        user_field = self.fields["user"]
        assert isinstance(user_field, forms.ModelChoiceField)
        # ほかの担当者に紐づいていないユーザー（と、いま紐づいているユーザー）だけ選べる
        free = User.objects.filter(staff__isnull=True, is_active=True)
        if self.instance.user_id is not None:
            free = free | User.objects.filter(pk=self.instance.user_id)
        user_field.queryset = free.order_by("username")
        user_field.help_text = _("The login user of this staff member (optional).")
        office_field = self.fields["office"]
        assert isinstance(office_field, forms.ModelChoiceField)
        office_field.queryset = Office.objects.filter(is_active=True).order_by("pk")
        self.style_fields()


class OrganizationForm(StyledFormMixin, forms.ModelForm[Organization]):
    class Meta:
        model = Organization
        fields = (
            "name",
            "name_kana",
            "registration_number",
            "registered_on",
            "support_start_planned_on",
            "registration_expires_on",
            "representative_title",
            "representative_name",
            "representative_name_kana",
            "postal_code",
            "address",
            "phone_number",
            "food_council_joined_on",
            "court_name",
            "signed_copy_due_days",
        )
        widgets = {
            "registered_on": forms.DateInput,
            "support_start_planned_on": forms.DateInput,
            "registration_expires_on": forms.DateInput,
            "food_council_joined_on": forms.DateInput,
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()


class CertificateForm(StyledFormMixin, forms.Form):
    """構成員証明書の差し替え（添付の検査は T03 の services.attachments）。"""

    file = forms.FileField(label=_("file"))
    valid_until = forms.DateField(label=_("valid until"), required=False, widget=forms.DateInput)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()
        policy = attachments.policy_for(AttachmentKind.FOOD_COUNCIL_CERTIFICATE)
        self.fields["file"].help_text = _("Allowed: %(extensions)s, up to %(size)s MB.") % {
            "extensions": ", ".join(sorted(policy.extensions)),
            "size": policy.max_bytes // (1024 * 1024),
        }

    def clean_file(self) -> Any:
        file = self.cleaned_data["file"]
        attachments.validate_upload(AttachmentKind.FOOD_COUNCIL_CERTIFICATE, file)
        return file


class OfficeForm(StyledFormMixin, forms.ModelForm[Office]):
    class Meta:
        model = Office
        fields = ("name", "postal_code", "address", "phone_number", "support_manager", "is_active")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        manager = self.fields["support_manager"]
        assert isinstance(manager, forms.ModelChoiceField)
        manager.queryset = organization.manager_choices(self.instance)
        manager.help_text = _(
            "Printed as the support manager in form 1-17 (section III, item 7) for workers whose "
            "staff member belongs to this office. Staff members of this office marked as support "
            "managers can be chosen (register the office first)."
        )
        self.style_fields()

    def clean_support_manager(self) -> Any:
        staff = self.cleaned_data.get("support_manager")
        organization.validate_support_manager(self.instance, staff)
        return staff


def _content_type_label(content_type: ContentType) -> str:
    model = content_type.model_class()
    return str(model._meta.verbose_name) if model is not None else content_type.model


class AuditSearchForm(StyledFormMixin, forms.Form):
    """監査ログの検索（GET）。既定では「監査ログの閲覧」を除く。"""

    actor = forms.ModelChoiceField(
        label=_("actor"),
        queryset=User.objects.order_by("username"),
        required=False,
        empty_label=_("All users"),
    )
    action = forms.ChoiceField(
        label=_("action"), required=False, choices=[("", _("All actions")), *AuditAction.choices]
    )
    content_type = forms.ModelChoiceField(
        label=_("target type"),
        queryset=ContentType.objects.none(),
        required=False,
        empty_label=_("All types"),
    )
    target = forms.CharField(
        label=_("target"),
        required=False,
        max_length=100,
        help_text=_("Part of the name, or the ID."),
    )
    date_from = forms.DateField(label=_("from"), required=False, widget=forms.DateInput)
    date_to = forms.DateField(label=_("to"), required=False, widget=forms.DateInput)
    include_audit_views = forms.BooleanField(
        label=_("Include viewing of audit logs"), required=False
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        labels = sorted(audit.AUDITED_MODELS | audit.VIEW_AUDITED_MODELS)
        ids = [
            ContentType.objects.get_by_natural_key(*label.split(".")).pk
            for label in labels
            if ContentType.objects.filter(
                app_label=label.split(".")[0], model=label.split(".")[1]
            ).exists()
        ]
        content_type = self.fields["content_type"]
        assert isinstance(content_type, forms.ModelChoiceField)
        content_type.queryset = ContentType.objects.filter(pk__in=ids).order_by(
            "app_label", "model"
        )
        content_type.label_from_instance = _content_type_label  # type: ignore[assignment,method-assign]
        self.style_fields()

    def to_query(self) -> AuditQuery:
        self.is_valid()
        data = self.cleaned_data
        actor = data.get("actor")
        content_type = data.get("content_type")
        date_from: datetime.date | None = data.get("date_from")
        date_to: datetime.date | None = data.get("date_to")
        return AuditQuery(
            actor_id=actor.pk if actor else None,
            action=data.get("action") or "",
            content_type_id=content_type.pk if content_type else None,
            target=data.get("target") or "",
            date_from=date_from,
            date_to=date_to,
            include_audit_views=bool(data.get("include_audit_views")),
        )
