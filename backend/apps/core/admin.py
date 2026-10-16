from typing import Any, TypeVar

from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.models import Group
from django.contrib.contenttypes.admin import GenericTabularInline
from django.db.models import Model, QuerySet
from django.forms import ModelForm
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _
from django_otp.plugins.otp_totp.models import TOTPDevice

from services.attachments import fill_file_metadata, validate_upload
from services.mfa import reset_mfa
from services.users import LastAdminError, deactivate_user, reactivate_user

from .models import (
    Attachment,
    Office,
    Organization,
    Role,
    RolePermission,
    Staff,
    SupportLanguage,
    TrackedModel,
    User,
    UserRole,
)

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


admin.site.unregister(Group)  # Django 標準のグループは使わない（権限はロールで表す）
admin.site.unregister(TOTPDevice)  # 端末の直接編集はさせない（MFA リセットのアクションで行う）


class UserRoleInline(admin.TabularInline[UserRole, User]):
    model = UserRole
    fk_name = "user"
    extra = 0
    readonly_fields = ("created_at", "created_by")


@admin.register(User)
class UserAdmin(TrackedModelAdmin[User], DjangoUserAdmin[User]):
    """ユーザーは削除しない。is_active は無効化・再有効化のアクションでだけ変える。"""

    list_display = ("username", "email", "last_name", "first_name", "is_active", "role_names")
    list_filter = ("is_active", "user_roles__role")
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        (_("Personal info"), {"fields": ("last_name", "first_name", "email")}),
        (
            _("Status"),
            {
                "fields": (
                    "is_active",
                    "deactivated_at",
                    "deactivated_by",
                    "last_login",
                    "date_joined",
                )
            },
        ),
    )
    readonly_fields = ("is_active", "deactivated_at", "deactivated_by", "last_login", "date_joined")
    inlines = [UserRoleInline]
    actions = ["deactivate", "reactivate", "reset_mfa"]

    def get_inlines(self, request: HttpRequest, obj: User | None) -> list[Any]:
        return [] if obj is None else list(self.inlines)

    @admin.display(description=_("roles"))
    def role_names(self, obj: User) -> str:
        return ", ".join(str(user_role.role) for user_role in obj.user_roles.all())

    def get_queryset(self, request: HttpRequest) -> QuerySet[User]:
        return super().get_queryset(request).prefetch_related("user_roles__role")

    def has_delete_permission(self, request: HttpRequest, obj: User | None = None) -> bool:
        return False

    @admin.action(description=_("Deactivate selected users"))
    def deactivate(self, request: HttpRequest, queryset: QuerySet[User]) -> None:
        actor = request.user if isinstance(request.user, User) else None
        for user in queryset.filter(is_active=True):
            try:
                deactivate_user(user, by=actor)
            except LastAdminError:
                self.message_user(
                    request,
                    _("%(user)s is the last active admin and cannot be deactivated.")
                    % {"user": user.username},
                    messages.ERROR,
                )

    @admin.action(description=_("Reactivate selected users"))
    def reactivate(self, request: HttpRequest, queryset: QuerySet[User]) -> None:
        actor = request.user if isinstance(request.user, User) else None
        for user in queryset.filter(is_active=False):
            reactivate_user(user, by=actor)

    @admin.action(description=_("Reset two-factor authentication of selected users"))
    def reset_mfa(self, request: HttpRequest, queryset: QuerySet[User]) -> None:
        """端末紛失時。TOTP と回復用コードを削除し、次回ログインで再設定させる。"""
        actor = request.user if isinstance(request.user, User) else None
        for user in queryset:
            reset_mfa(user, by=actor)  # TODO(T05): 監査ログ記録は services.mfa.reset_mfa 内で行う


class RolePermissionInline(admin.TabularInline[RolePermission, Role]):
    model = RolePermission
    extra = 0


@admin.register(Role)
class RoleAdmin(TrackedModelAdmin[Role]):
    list_display = ("name", "code", "data_scope")
    readonly_fields = TRACKED_READONLY_FIELDS
    inlines = [RolePermissionInline]

    def has_delete_permission(self, request: HttpRequest, obj: Role | None = None) -> bool:
        return False


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
