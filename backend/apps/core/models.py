"""自機関側のマスタと監査ログ。

仕様書第7章 organization / users / offices / staff / attachments / audit_logs。
"""

from typing import Any

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_otp.models import Device, ThrottlingMixin, TimestampMixin

from services import recovery_codes
from services.files import attachment_upload_to, logo_upload_to, validate_logo


class User(AbstractUser):  # type: ignore[django-manager-missing]  # django-otp の端末は型情報なし
    """ログインユーザー。権限はロール（UserRole）だけで決まり、superuser は使わない。

    ユーザーは削除せず無効化する（services.users.deactivate_user）。
    """

    deactivated_at = models.DateTimeField(_("deactivated at"), null=True, blank=True)
    deactivated_by = models.ForeignKey(
        "self",
        verbose_name=_("deactivated by"),
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = _("user")
        verbose_name_plural = _("users")
        constraints = [
            # superuser はロールによる権限判定を素通りするため使わない。管理者は Role で表す。
            models.CheckConstraint(condition=Q(is_superuser=False), name="core_user_no_superuser"),
        ]


class TrackedModel(models.Model):
    """全テーブル共通の作成/更新日時・作成/更新者（第7章）。

    ユーザーは削除せず無効化する運用のため、作成者・更新者は PROTECT にする。
    """

    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("created by"),
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
        editable=False,
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("updated by"),
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
        editable=False,
    )

    class Meta:
        abstract = True


class AttachmentKind(models.TextChoices):
    FOOD_COUNCIL_CERTIFICATE = (
        "food_council_certificate",
        _("Food Industry SSW Council membership certificate"),
    )


class Attachment(TrackedModel):
    """添付ファイル。上書き・削除せず、差し替えは新しい行を追加する。

    アップロード者は created_by。ファイルの検査とハッシュ計算は services.attachments で行う。
    """

    kind = models.CharField(_("kind"), max_length=64, choices=AttachmentKind.choices)
    file = models.FileField(_("file"), upload_to=attachment_upload_to, max_length=255)
    original_filename = models.CharField(_("original filename"), max_length=255, editable=False)
    size = models.PositiveBigIntegerField(_("size (bytes)"), editable=False)
    sha256 = models.CharField("SHA-256", max_length=64, editable=False)
    valid_until = models.DateField(_("valid until"), null=True, blank=True)

    content_type = models.ForeignKey(
        ContentType, verbose_name=_("target type"), on_delete=models.PROTECT
    )
    object_id = models.PositiveBigIntegerField(_("target ID"))
    target = GenericForeignKey("content_type", "object_id")

    class Meta:
        verbose_name = _("attachment")
        verbose_name_plural = _("attachments")
        indexes = [models.Index(fields=["content_type", "object_id"])]

    def __str__(self) -> str:
        return f"{self.get_kind_display()}: {self.original_filename}"


class Organization(TrackedModel):
    """自機関（登録支援機関）。1 行のみ。

    自社名・登録番号・ロゴなど自社固有の値の置き場（制約9）。
    """

    # 2 行目を DB で拒否するための列。常に True で一意。
    singleton = models.BooleanField(default=True, editable=False, unique=True)

    name = models.CharField(_("name"), max_length=200)
    registration_number = models.CharField(_("registration number"), max_length=50)
    registration_expires_on = models.DateField(_("registration expiry date"))
    postal_code = models.CharField(_("postal code"), max_length=8)
    address = models.CharField(_("address"), max_length=255)
    food_council_joined_on = models.DateField(
        _("Food Industry SSW Council joined on"), null=True, blank=True
    )
    logo = models.FileField(
        _("logo"),
        upload_to=logo_upload_to,
        validators=[validate_logo],
        max_length=255,
        blank=True,
    )
    attachments = GenericRelation(Attachment)

    class Meta:
        verbose_name = _("organization")
        verbose_name_plural = _("organization")
        constraints = [
            models.CheckConstraint(condition=Q(singleton=True), name="core_organization_singleton"),
        ]

    def __str__(self) -> str:
        return self.name


class Office(TrackedModel):
    """自機関の事務所。2027年4月要件の「事務所ごと」の単位。閉鎖時は削除せず無効化する。"""

    name = models.CharField(_("name"), max_length=200)
    postal_code = models.CharField(_("postal code"), max_length=8)
    address = models.CharField(_("address"), max_length=255)
    phone_number = models.CharField(_("phone number"), max_length=20, blank=True)
    is_active = models.BooleanField(_("active"), default=True)

    class Meta:
        verbose_name = _("office")
        verbose_name_plural = _("offices")

    def __str__(self) -> str:
        return self.name


class SupportLanguage(TrackedModel):
    """支援言語の選択肢（制約9）。初期値はデータマイグレーションで投入する。"""

    code = models.CharField(_("code"), max_length=16, unique=True)
    name = models.CharField(_("display name"), max_length=50)
    sort_order = models.PositiveSmallIntegerField(_("sort order"), default=0)
    is_active = models.BooleanField(_("active"), default=True)

    class Meta:
        verbose_name = _("support language")
        verbose_name_plural = _("support languages")
        ordering = ["sort_order", "code"]

    def __str__(self) -> str:
        return self.name


class EmploymentType(models.TextChoices):
    FULL_TIME = "full_time", _("full-time")
    PART_TIME = "part_time", _("part-time")


class Staff(TrackedModel):
    """支援責任者・支援担当者。ログインしない担当者もいるため User との紐づけは任意。"""

    name = models.CharField(_("full name"), max_length=100)
    name_kana = models.CharField(_("full name (kana)"), max_length=100, blank=True)
    is_support_manager = models.BooleanField(_("support manager"), default=False)
    is_support_staff = models.BooleanField(_("support staff"), default=False)
    employment_type = models.CharField(
        _("employment type"), max_length=16, choices=EmploymentType.choices
    )
    office = models.ForeignKey(
        Office,
        verbose_name=_("office"),
        on_delete=models.PROTECT,
        related_name="staff_members",
    )
    training_completed_on = models.DateField(
        _("training course completed on"), null=True, blank=True
    )
    has_life_consultation_experience = models.BooleanField(
        _("has life consultation experience"), default=False
    )
    life_consultation_experience_note = models.TextField(
        _("life consultation experience (details and period)"), blank=True
    )
    languages = models.ManyToManyField(
        SupportLanguage, verbose_name=_("languages"), related_name="staff_members", blank=True
    )
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        verbose_name=_("login user"),
        on_delete=models.PROTECT,
        related_name="staff",
        null=True,
        blank=True,
    )
    is_active = models.BooleanField(_("active"), default=True)

    class Meta:
        verbose_name = _("staff member")
        verbose_name_plural = _("staff members")
        constraints = [
            models.CheckConstraint(
                condition=Q(is_support_manager=True) | Q(is_support_staff=True),
                name="core_staff_has_role",
                violation_error_message=_(
                    "Select at least one of support manager or support staff."
                ),
            ),
        ]

    def __str__(self) -> str:
        return self.name


class Action(models.TextChoices):
    """権限判定の操作（services.authorization）。機能を追加するタスクで行を足す。"""

    ADMIN_ACCESS = "admin.access", _("access the management site")
    USERS_MANAGE = "users.manage", _("manage users, roles, invitations and MFA")
    ORGANIZATION_VIEW = "organization.view", _("view organization, offices and staff")
    ORGANIZATION_MANAGE = "organization.manage", _("edit organization, offices and staff")
    AUDIT_VIEW = "audit.view", _("view audit logs")


class DataScope(models.TextChoices):
    """ロールが見られるデータの範囲。ポータル追加時に「本人のみ」「自社機関のみ」を足す。"""

    ALL = "all", _("all data")


class Role(TrackedModel):
    """ロール（第4章）。ロールの追加は行の追加で行い、コードは変えない。"""

    code = models.CharField(_("code"), max_length=32, unique=True)
    name = models.CharField(_("name"), max_length=50)
    data_scope = models.CharField(
        _("data scope"), max_length=32, choices=DataScope.choices, default=DataScope.ALL
    )

    class Meta:
        verbose_name = _("role")
        verbose_name_plural = _("roles")

    def __str__(self) -> str:
        return self.name


class RolePermission(TrackedModel):
    """ロール×操作。権限表をデータで持つ。"""

    role = models.ForeignKey(
        Role, verbose_name=_("role"), on_delete=models.CASCADE, related_name="permissions"
    )
    action = models.CharField(_("action"), max_length=64, choices=Action.choices)

    class Meta:
        verbose_name = _("role permission")
        verbose_name_plural = _("role permissions")
        constraints = [
            models.UniqueConstraint(fields=["role", "action"], name="core_rolepermission_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.role}: {self.get_action_display()}"


class UserRole(TrackedModel):
    """ユーザー×ロール（第7章 user_roles）。"""

    user = models.ForeignKey(
        User, verbose_name=_("user"), on_delete=models.PROTECT, related_name="user_roles"
    )
    role = models.ForeignKey(
        Role, verbose_name=_("role"), on_delete=models.PROTECT, related_name="user_roles"
    )

    class Meta:
        verbose_name = _("user role")
        verbose_name_plural = _("user roles")
        constraints = [
            models.UniqueConstraint(fields=["user", "role"], name="core_userrole_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.user}: {self.role}"


class RecoveryCodeDevice(TimestampMixin, ThrottlingMixin, Device):  # type: ignore[misc]
    """回復用コード（TOTP 端末を紛失したときの代替）。

    django-otp の端末として扱い、セッションを検証済みにできる。

    コードは平文で保存しない（RecoveryCode.code_hash）。使用済みのコードは used_at を入れて残す。
    """

    class Meta:
        verbose_name = _("recovery code set")
        verbose_name_plural = _("recovery code sets")

    def get_throttle_factor(self) -> int:
        return getattr(settings, "OTP_RECOVERY_CODE_THROTTLE_FACTOR", 1)

    def verify_token(self, token: str) -> bool:
        allowed, _info = self.verify_is_allowed()
        if not allowed:
            return False
        match = None
        if recovery_codes.looks_like_recovery_code(token):
            match = self.codes.filter(
                code_hash=recovery_codes.hash_code(token), used_at__isnull=True
            ).first()
        if match is None:
            self.throttle_increment()
            return False
        match.used_at = timezone.now()
        match.save(update_fields=["used_at"])
        self.throttle_reset(commit=False)
        self.set_last_used_timestamp(commit=False)
        self.save()
        return True


class RecoveryCode(models.Model):
    device = models.ForeignKey(RecoveryCodeDevice, on_delete=models.CASCADE, related_name="codes")
    code_hash = models.CharField(max_length=64)
    used_at = models.DateTimeField(_("used at"), null=True, blank=True)

    class Meta:
        verbose_name = _("recovery code")
        verbose_name_plural = _("recovery codes")
        constraints = [
            models.UniqueConstraint(
                fields=["device", "code_hash"], name="core_recoverycode_unique"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.device}: {'used' if self.used_at else 'unused'}"


class Invitation(TrackedModel):
    """招待（第7章「招待状態」）。

    トークンは SHA-256 だけを保存し、平文はメールの URL にだけ載せる。
    """

    user = models.ForeignKey(
        User, verbose_name=_("user"), on_delete=models.PROTECT, related_name="invitations"
    )
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    expires_at = models.DateTimeField(_("expires at"))
    accepted_at = models.DateTimeField(_("accepted at"), null=True, blank=True)
    revoked_at = models.DateTimeField(_("revoked at"), null=True, blank=True)

    class Meta:
        verbose_name = _("invitation")
        verbose_name_plural = _("invitations")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.user}: {self.created_at:%Y-%m-%d %H:%M}"


class AuditAction(models.TextChoices):
    VIEW = "view", _("view")
    CREATE = "create", _("create")
    UPDATE = "update", _("update")
    DELETE = "delete", _("delete")
    EXPORT = "export", _("export")
    GENERATE = "generate", _("generate document")
    DOWNLOAD = "download", _("download")
    INVITE = "invite", _("invite user")
    DEACTIVATE = "deactivate", _("deactivate user")
    REACTIVATE = "reactivate", _("reactivate user")
    MFA_RESET = "mfa_reset", _("reset two-factor authentication")


class AppendOnlyError(Exception):
    """監査ログは追記専用で、更新・削除できない。"""


class AuditLogQuerySet(models.QuerySet["AuditLog"]):
    def update(self, **kwargs: object) -> int:
        raise AppendOnlyError("audit logs cannot be updated")

    def delete(self) -> tuple[int, dict[str, int]]:
        raise AppendOnlyError("audit logs cannot be deleted")


class AuditLog(models.Model):
    """監査ログ（第8章）。追記専用（モデルと DB トリガーで更新・削除を禁止）。

    TrackedModel は継承しない（ADR-0002）。作成日時・作成者は occurred_at・actor が担う。
    記録は services.audit から行う。
    """

    occurred_at = models.DateTimeField(_("occurred at"), default=timezone.now, db_index=True)
    actor = models.ForeignKey(
        User,
        verbose_name=_("actor"),
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
    )
    actor_username = models.CharField(_("actor username"), max_length=150, blank=True)
    action = models.CharField(_("action"), max_length=32, choices=AuditAction.choices)
    content_type = models.ForeignKey(
        ContentType,
        verbose_name=_("target type"),
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    object_id = models.CharField(_("target ID"), max_length=64, blank=True)
    object_repr = models.CharField(_("target"), max_length=255, blank=True)
    target = GenericForeignKey("content_type", "object_id")
    changes = models.JSONField(_("changes"), default=dict, blank=True, encoder=DjangoJSONEncoder)
    context = models.JSONField(_("context"), default=dict, blank=True, encoder=DjangoJSONEncoder)
    ip_address = models.GenericIPAddressField(_("IP address"), null=True, blank=True)
    path = models.CharField(_("path"), max_length=255, blank=True)

    objects = AuditLogQuerySet.as_manager()

    class Meta:
        verbose_name = _("audit log")
        verbose_name_plural = _("audit logs")
        ordering = ["-occurred_at", "-id"]
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
            models.Index(fields=["actor", "occurred_at"]),
            models.Index(fields=["action", "occurred_at"]),
        ]

    def __str__(self) -> str:
        return (
            f"{self.occurred_at:%Y-%m-%d %H:%M:%S} {self.get_action_display()} {self.object_repr}"
        )

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise AppendOnlyError("audit logs cannot be updated")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise AppendOnlyError("audit logs cannot be deleted")
