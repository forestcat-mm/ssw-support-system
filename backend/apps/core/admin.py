from typing import Any, TypeVar

from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.contenttypes.admin import GenericTabularInline
from django.db.models import Model
from django.forms import ModelForm
from django.http import HttpRequest

from services.attachments import fill_file_metadata, validate_upload

from .models import Attachment, Office, Organization, Staff, SupportLanguage, TrackedModel, User

TRACKED_READONLY_FIELDS = ("created_at", "created_by", "updated_at", "updated_by")

_M = TypeVar("_M", bound=Model)


class TrackedModelAdmin(admin.ModelAdmin[_M]):
    """保存時に作成者・更新者をログインユーザーで埋める（インラインを含む）。"""

    def save_model(self, request: HttpRequest, obj: _M, form: ModelForm[_M], change: bool) -> None:
        _stamp(obj, request)
        super().save_model(request, obj, form, change)

    def save_formset(
        self, request: HttpRequest, form: ModelForm[_M], formset: Any, change: bool
    ) -> None:
        for deleted in formset.deleted_objects if formset.can_delete else []:
            deleted.delete()
        for instance in formset.save(commit=False):
            _stamp(instance, request)
            instance.save()
        formset.save_m2m()


def _stamp(obj: Model, request: HttpRequest) -> None:
    if not isinstance(obj, TrackedModel) or not isinstance(request.user, User):
        return
    if obj.pk is None:
        obj.created_by = request.user
    obj.updated_by = request.user


@admin.register(User)
class UserAdmin(DjangoUserAdmin[User]):
    pass


class AttachmentInlineForm(forms.ModelForm[Attachment]):
    """新規行はファイルを検査してハッシュ等を埋める。既存行は種別・ファイルを変更不可にする。"""

    class Meta:
        model = Attachment
        fields = ("kind", "file", "valid_until")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self.instance.pk is not None:
            self.fields["kind"].disabled = True
            self.fields["file"].disabled = True

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        file = cleaned.get("file")
        kind = cleaned.get("kind")
        if self.instance.pk is None and file and kind:
            try:
                validate_upload(kind, file)
            except forms.ValidationError as error:
                self.add_error("file", error)
            else:
                fill_file_metadata(self.instance, file)
        return cleaned


class AttachmentInline(GenericTabularInline):
    model = Attachment
    form = AttachmentInlineForm
    extra = 0
    can_delete = False
    readonly_fields = ("original_filename", "size", "created_at", "created_by")


@admin.register(Attachment)
class AttachmentAdmin(TrackedModelAdmin[Attachment]):
    """一覧・参照用。追加は対象（自機関など）の画面から行い、削除はしない。"""

    list_display = ("kind", "original_filename", "content_type", "object_id", "valid_until")
    list_filter = ("kind",)
    fields = (
        "kind",
        "file",
        "original_filename",
        "size",
        "sha256",
        "valid_until",
        "content_type",
        "object_id",
        *TRACKED_READONLY_FIELDS,
    )
    readonly_fields = (
        "kind",
        "file",
        "original_filename",
        "size",
        "sha256",
        "content_type",
        "object_id",
        *TRACKED_READONLY_FIELDS,
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Attachment | None = None) -> bool:
        return False


@admin.register(Organization)
class OrganizationAdmin(TrackedModelAdmin[Organization]):
    list_display = ("name", "registration_number", "registration_expires_on")
    readonly_fields = TRACKED_READONLY_FIELDS
    inlines = [AttachmentInline]

    def has_add_permission(self, request: HttpRequest) -> bool:
        return super().has_add_permission(request) and not Organization.objects.exists()

    def has_delete_permission(self, request: HttpRequest, obj: Organization | None = None) -> bool:
        return False


@admin.register(Office)
class OfficeAdmin(TrackedModelAdmin[Office]):
    list_display = ("name", "address", "is_active")
    list_filter = ("is_active",)
    readonly_fields = TRACKED_READONLY_FIELDS


@admin.register(SupportLanguage)
class SupportLanguageAdmin(TrackedModelAdmin[SupportLanguage]):
    list_display = ("name", "code", "sort_order", "is_active")
    readonly_fields = TRACKED_READONLY_FIELDS


@admin.register(Staff)
class StaffAdmin(TrackedModelAdmin[Staff]):
    list_display = (
        "name",
        "office",
        "is_support_manager",
        "is_support_staff",
        "employment_type",
        "training_completed_on",
        "is_active",
    )
    list_filter = (
        "office",
        "employment_type",
        "is_support_manager",
        "is_support_staff",
        "is_active",
    )
    search_fields = ("name", "name_kana")
    filter_horizontal = ("languages",)
    readonly_fields = TRACKED_READONLY_FIELDS
