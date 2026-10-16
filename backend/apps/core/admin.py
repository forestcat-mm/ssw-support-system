from typing import Any, TypeVar

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.utils import unquote
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.models import Group
from django.contrib.contenttypes.admin import GenericTabularInline
from django.db.models import Model, QuerySet
from django.forms import ModelForm
from django.http import HttpRequest, HttpResponse
from django.urls import reverse
from django.utils.html import format_html, format_html_join
from django.utils.safestring import SafeString
from django.utils.translation import gettext_lazy as _
from django_otp.plugins.otp_totp.models import TOTPDevice

from services import audit
from services.attachments import fill_file_metadata, validate_upload
from services.invitations import InvitationStatus, invitation_status, invite_user, resend_invitation
from services.mfa import reset_mfa
from services.users import LastAdminError, deactivate_user, reactivate_user

from .admin_filters import DateRangeFilter
from .forms import InvitationForm
from .models import (
    Attachment,
    AttachmentKind,
    AuditLog,
    Country,
    Invitation,
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
from .views import invitation_url_builder

TRACKED_READONLY_FIELDS: tuple[str, ...] = ("created_at", "created_by", "updated_at", "updated_by")

_M = TypeVar("_M", bound=Model)


class TrackedModelAdmin(admin.ModelAdmin[_M]):
    """保存時に作成者・更新者をログインユーザーで埋める（インラインを含む）。

    個人情報を含むモデル（services.audit.VIEW_AUDITED_MODELS）は、詳細画面の表示を監査ログに残す。
    """

    def change_view(
        self,
        request: HttpRequest,
        object_id: str,
        form_url: str = "",
        extra_context: dict[str, Any] | None = None,
    ) -> HttpResponse:
        response = super().change_view(request, object_id, form_url, extra_context)
        if request.method == "GET" and response.status_code == 200:
            obj = self.get_object(request, unquote(object_id))
            if obj is not None:
                audit.record_view(obj)
        return response

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


class InvitationInline(admin.TabularInline[Invitation, User]):
    """招待の履歴（参照のみ）。"""

    model = Invitation
    fk_name = "user"
    extra = 0
    can_delete = False
    fields = ("created_at", "created_by", "expires_at", "accepted_at", "revoked_at")
    readonly_fields = fields

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


class UserRoleInline(admin.TabularInline[UserRole, User]):
    model = UserRole
    fk_name = "user"
    extra = 0
    readonly_fields = ("created_at", "created_by")


@admin.register(User)
class UserAdmin(TrackedModelAdmin[User], DjangoUserAdmin[User]):
    """ユーザーは削除しない。is_active は無効化・再有効化のアクションでだけ変える。"""

    list_display = (
        "username",
        "email",
        "last_name",
        "first_name",
        "is_active",
        "role_names",
        "invitation",
    )
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
    inlines = [UserRoleInline, InvitationInline]
    actions = ["deactivate", "reactivate", "reset_mfa", "resend_invitation"]
    # 追加はパスワードを入力させず招待メールで行う
    add_form = InvitationForm
    add_fieldsets = (
        (None, {"fields": ("username", "email", "last_name", "first_name", "roles", "staff")}),
    )

    def get_inlines(self, request: HttpRequest, obj: User | None) -> list[Any]:
        return [] if obj is None else list(self.inlines)

    def save_model(self, request: HttpRequest, obj: User, form: Any, change: bool) -> None:
        if change:
            super().save_model(request, obj, form, change)
            return
        invite_user(
            obj,
            roles=form.cleaned_data["roles"],
            staff=form.cleaned_data["staff"],
            by=request.user if isinstance(request.user, User) else None,
            build_url=invitation_url_builder(request),
        )
        self.message_user(
            request, _("An invitation email was sent to %(email)s.") % {"email": obj.email}
        )

    @admin.display(description=_("invitation"))
    def invitation(self, obj: User) -> str:
        return {
            InvitationStatus.NONE: "-",
            InvitationStatus.PENDING: str(_("pending")),
            InvitationStatus.EXPIRED: str(_("expired")),
            InvitationStatus.ACCEPTED: str(_("accepted")),
        }[invitation_status(obj)]

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

    @admin.action(description=_("Resend invitation to selected users"))
    def resend_invitation(self, request: HttpRequest, queryset: QuerySet[User]) -> None:
        """パスワード未設定（未受諾）の有効なユーザーにだけ送り直す。古いリンクは無効になる。"""
        actor = request.user if isinstance(request.user, User) else None
        build_url = invitation_url_builder(request)
        sent = 0
        for user in queryset.filter(is_active=True):
            if user.has_usable_password():
                continue
            resend_invitation(user, by=actor, build_url=build_url)
            sent += 1
        self.message_user(request, _("Invitations sent: %(count)d") % {"count": sent})

    @admin.action(description=_("Reset two-factor authentication of selected users"))
    def reset_mfa(self, request: HttpRequest, queryset: QuerySet[User]) -> None:
        """端末紛失時。TOTP と回復用コードを削除し、次回ログインで再設定させる。"""
        actor = request.user if isinstance(request.user, User) else None
        for user in queryset:
            reset_mfa(user, by=actor)  # 監査ログは reset_mfa の中で記録する


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


class DownloadLinkWidget(forms.Widget):
    """登録済みの添付は、公開 URL ではなく認可・監査付きのダウンロード画面へのリンクで見せる。"""

    def __init__(self, attachment: Attachment) -> None:
        super().__init__()
        self.attachment = attachment

    def render(self, name: str, value: Any, attrs: Any = None, renderer: Any = None) -> SafeString:
        return download_link(self.attachment)


def download_link(attachment: Attachment) -> SafeString:
    url = reverse("attachment_download", args=[attachment.pk])
    return format_html('<a href="{}">{}</a>', url, attachment.original_filename)


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
            self.fields["file"].widget = DownloadLinkWidget(self.instance)

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
    """添付欄。kinds を指定すると、その画面で使う種別だけを選べる。"""

    model = Attachment
    form = AttachmentInlineForm
    extra = 0
    can_delete = False
    readonly_fields = ("original_filename", "size", "created_at", "created_by")
    kinds: tuple[AttachmentKind, ...] = ()

    def formfield_for_choice_field(self, db_field: Any, request: HttpRequest, **kwargs: Any) -> Any:
        if db_field.name == "kind" and self.kinds:
            kwargs["choices"] = [(kind.value, kind.label) for kind in self.kinds]
        return super().formfield_for_choice_field(db_field, request, **kwargs)


@admin.register(Attachment)
class AttachmentAdmin(TrackedModelAdmin[Attachment]):
    """一覧・参照用。追加は対象（自機関など）の画面から行い、削除はしない。"""

    list_display = ("kind", "original_filename", "content_type", "object_id", "valid_until")
    list_filter = ("kind",)
    fields = (
        "kind",
        "download",
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
        "download",
        "original_filename",
        "size",
        "sha256",
        "content_type",
        "object_id",
        *TRACKED_READONLY_FIELDS,
    )

    @admin.display(description=_("file"))
    def download(self, obj: Attachment) -> str:
        return download_link(obj)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Attachment | None = None) -> bool:
        return False


class OrganizationAttachmentInline(AttachmentInline):
    kinds = (AttachmentKind.FOOD_COUNCIL_CERTIFICATE,)


@admin.register(Organization)
class OrganizationAdmin(TrackedModelAdmin[Organization]):
    list_display = ("name", "registration_number", "registration_expires_on")
    readonly_fields = TRACKED_READONLY_FIELDS
    inlines = [OrganizationAttachmentInline]

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


@admin.register(Country)
class CountryAdmin(TrackedModelAdmin[Country]):
    list_display = ("name", "code", "sort_order", "is_active")
    list_editable = ("sort_order", "is_active")
    search_fields = ("name", "code")
    readonly_fields = TRACKED_READONLY_FIELDS

    def has_delete_permission(self, request: HttpRequest, obj: Country | None = None) -> bool:
        return False


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


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin[AuditLog]):
    """監査ログ（読み取り専用。閲覧は audit.view を持つ管理者のみ）。"""

    list_display = (
        "occurred_at",
        "action",
        "actor_username",
        "content_type",
        "object_repr",
        "ip_address",
    )
    list_filter = (
        ("occurred_at", DateRangeFilter),
        "action",
        "actor",
        "content_type",
    )
    search_fields = ("actor_username", "object_repr", "object_id", "path")
    date_hierarchy = "occurred_at"
    list_select_related = ("content_type",)
    fields = (
        "occurred_at",
        "action",
        "actor",
        "actor_username",
        "content_type",
        "object_id",
        "object_repr",
        "changes_table",
        "context",
        "ip_address",
        "path",
    )
    readonly_fields = fields

    @admin.display(description=_("changes"))
    def changes_table(self, obj: AuditLog) -> str:
        if not obj.changes:
            return "-"
        rows = format_html_join(
            "",
            "<tr><td>{}</td><td>{}</td><td>{}</td></tr>",
            ((name, values[0], values[1]) for name, values in sorted(obj.changes.items())),
        )
        return format_html(
            "<table><thead><tr><th>{}</th><th>{}</th><th>{}</th></tr></thead>"
            "<tbody>{}</tbody></table>",
            _("field"),
            _("before"),
            _("after"),
            rows,
        )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: AuditLog | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: AuditLog | None = None) -> bool:
        return False
