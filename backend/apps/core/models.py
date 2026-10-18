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
from django.db.models.fields.files import FieldFile
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
    MEMBERSHIP_CERTIFICATE = (
        "membership_certificate",
        _("Membership certificate of host organization (council / registered corporation)"),
    )
    COOPERATION_CONFIRMATION = "cooperation_confirmation", _("Letter of cooperation confirmation")
    SUPPORT_CONTRACT = "support_contract", _("Support entrustment contract")
    EQUAL_PAY_EVIDENCE = (
        "equal_pay_evidence",
        _("Evidence of pay equal to or higher than Japanese workers"),
    )
    VARIABLE_HOURS_DOCUMENTS = (
        "variable_hours_documents",
        _("Annual calendar and labor-management agreement for variable working hours"),
    )
    # 雇用条件書 別紙2（無期転換後の労働条件。変更が「有」のとき。T28b、ADR-0016）
    INDEFINITE_CONVERSION_TERMS = (
        "indefinite_conversion_terms",
        _("Working conditions after conversion to an indefinite contract (form 1-6, annex 2)"),
    )
    SUPPORT_RECORD = (
        "support_record",
        _("Evidence of support (signed confirmation, photo, etc.)"),
    )
    # 署名済みの確認書（第6章「署名を要する書類の扱い」。段階3で生成履歴に紐づける。T21）
    PRE_GUIDANCE_CONFIRMATION = (
        "pre_guidance_confirmation",
        _("Signed confirmation of the pre-entry guidance (reference form 5-9)"),
    )
    ORIENTATION_CONFIRMATION = (
        "orientation_confirmation",
        _("Signed confirmation of the life orientation (reference form 5-8)"),
    )
    # 生成した帳票の署名済みのスキャン（記録の無い様式。第1-17号など。T33、ADR-0022）
    SIGNED_DOCUMENT = (
        "signed_document",
        _("Signed scan of a generated form"),
    )
    # 本人に交付した書類の署名済みの控え・受領の記録（T22、ADR-0010）
    DELIVERED_DOCUMENT = (
        "delivered_document",
        _("Signed copy or receipt of a document given to the worker"),
    )
    # 要配慮個人情報（健康診断の書類。外国人にだけ添付する。T23、ADR-0011）
    HEALTH_CHECK_RECORD = (
        "health_check_record",
        _("Health check record (reference form 1-3, with Japanese translation)"),
    )
    HEALTH_DECLARATION = (
        "health_declaration",
        _("Declaration of the examinee (reference form 1-3, attachment)"),
    )
    DOCTOR_OPINION = (
        "doctor_opinion",
        _("Doctor's opinion that the worker can work without problems"),
    )


# 要配慮の添付の種別（ダウンロードを担当者と管理者に限る。印は種別から自動で付く。ADR-0011）
SENSITIVE_KINDS: frozenset[str] = frozenset(
    {
        AttachmentKind.HEALTH_CHECK_RECORD,
        AttachmentKind.HEALTH_DECLARATION,
        AttachmentKind.DOCTOR_OPINION,
    }
)
# 要配慮の添付を付けられる対象（app_label, model_name）
SENSITIVE_TARGET = ("workers", "foreignworker")


class PrivateFieldFile(FieldFile):
    """URL を返さないファイル。中身は認可つきのダウンロード（AttachmentDownloadView）からだけ渡す。

    公開 URL も署名付き URL も作らせないため、url を使うと例外にする（ADR-0011）。
    """

    @property
    def url(self) -> str:
        raise PermissionError("Attachment files have no URL. Use the attachment download view.")


class PrivateFileField(models.FileField):
    attr_class = PrivateFieldFile


class Attachment(TrackedModel):
    """添付ファイル。上書き・削除せず、差し替えは新しい行を追加する。

    アップロード者は created_by。ファイルの検査とハッシュ計算は services.attachments で行う。
    """

    kind = models.CharField(_("kind"), max_length=64, choices=AttachmentKind.choices)
    file = PrivateFileField(_("file"), upload_to=attachment_upload_to, max_length=255)
    original_filename = models.CharField(_("original filename"), max_length=255, editable=False)
    size = models.PositiveBigIntegerField(_("size (bytes)"), editable=False)
    sha256 = models.CharField("SHA-256", max_length=64, editable=False)
    valid_until = models.DateField(_("valid until"), null=True, blank=True)
    # 要配慮（種別から保存のたびに決める）と保持期限の目安（運用で決める削除時期。T23）
    is_sensitive = models.BooleanField(_("sensitive"), default=False, editable=False)
    retention_until = models.DateField(_("keep until (guide)"), null=True, blank=True)
    # 取消（誤った添付。ファイルは削除せず、打ち消して残す。T18）
    voided_at = models.DateTimeField(_("voided at"), null=True, blank=True, editable=False)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("voided by"),
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
        editable=False,
    )
    void_reason = models.CharField(_("void reason"), max_length=200, blank=True, editable=False)

    content_type = models.ForeignKey(
        ContentType, verbose_name=_("target type"), on_delete=models.PROTECT
    )
    object_id = models.PositiveBigIntegerField(_("target ID"))
    target = GenericForeignKey("content_type", "object_id")

    class Meta:
        verbose_name = _("attachment")
        verbose_name_plural = _("attachments")
        indexes = [models.Index(fields=["content_type", "object_id"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(kind__in=sorted(SENSITIVE_KINDS), is_sensitive=True)
                | (~Q(kind__in=sorted(SENSITIVE_KINDS)) & Q(is_sensitive=False)),
                name="core_attachment_sensitive_follows_kind",
            ),
        ]

    def __str__(self) -> str:
        # 要配慮の添付はファイル名を出さない（監査ログの対象の表示・admin で使う。ADR-0011）
        if self.is_sensitive:
            return f"{self.get_kind_display()} #{self.pk}"
        return f"{self.get_kind_display()}: {self.original_filename}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        # 添付の対象は GenericRelation(Attachment) を持つモデルだけ。対象のモデルの閲覧権限で
        # ダウンロードを判定するため、対象のモデルはテストで権限の対応表との突き合わせを受ける
        # （tests/core/test_attachment.py。対応表に無いと誰もダウンロードできない）
        self.is_sensitive = self.kind in SENSITIVE_KINDS
        if self._state.adding:
            model = self.content_type.model_class()
            if model is None or not is_attachment_target(model):
                raise ValueError(f"{model} cannot have attachments (no GenericRelation)")
            label = (self.content_type.app_label, self.content_type.model)
            if self.is_sensitive and label != SENSITIVE_TARGET:
                raise ValueError("Sensitive attachments can be attached only to foreign workers")
        super().save(*args, **kwargs)

    @property
    def audit_masked_fields(self) -> frozenset[str]:
        """監査ログの差分で値を残さない列（要配慮の添付のファイル名）。"""
        return frozenset({"original_filename"}) if self.is_sensitive else frozenset()


def is_attachment_target(model: type[models.Model]) -> bool:
    """添付を付けられるモデル（GenericRelation(Attachment) を持つ）。"""
    return any(
        isinstance(field, GenericRelation) and field.related_model is Attachment
        for field in model._meta.get_fields()
    )


class Organization(TrackedModel):
    """自機関（登録支援機関）。1 行のみ。

    自社名・登録番号・ロゴなど自社固有の値の置き場（制約9）。
    """

    # 2 行目を DB で拒否するための列。常に True で一意。
    singleton = models.BooleanField(default=True, editable=False, unique=True)

    name = models.CharField(_("name"), max_length=200)
    # 支援計画書（参考様式第1-17号）Ⅲ欄３のふりがな（T31）
    name_kana = models.CharField(_("name (kana)"), max_length=200, blank=True)
    registration_number = models.CharField(_("registration number"), max_length=50)
    registration_expires_on = models.DateField(_("registration expiry date"))
    # 支援計画書（参考様式第1-17号）のⅢ欄などで使う（T17）
    registered_on = models.DateField(_("registration date"), null=True, blank=True)
    # 登録支援機関登録簿の「支援業務を開始する予定年月日」。第1-17号Ⅲ欄２の登録年月日の欄に書く
    # （記載例〔全部委託〕2 頁の吹き出し。T31-D12、ADR-0020）
    support_start_planned_on = models.DateField(
        _("planned start date of support work"), null=True, blank=True
    )
    representative_title = models.CharField(_("representative's title"), max_length=50, blank=True)
    representative_name = models.CharField(_("representative's name"), max_length=100, blank=True)
    representative_name_kana = models.CharField(
        _("representative's name (kana)"), max_length=100, blank=True
    )
    phone_number = models.CharField(_("phone number"), max_length=20, blank=True)
    postal_code = models.CharField(_("postal code"), max_length=8)
    address = models.CharField(_("address"), max_length=255)
    food_council_joined_on = models.DateField(
        _("Food Industry SSW Council joined on"), null=True, blank=True
    )
    # 支援委託契約書（第5-10号）第8条の専属的合意管轄の地方裁判所（地名。T37-D8、ADR-0026）
    court_name = models.CharField(
        _("district court with exclusive jurisdiction (place name)"),
        max_length=50,
        blank=True,
        help_text=_("Article 8 of the support entrustment contract (form 5-10). Example: Tokyo."),
    )
    # 署名済み書類の回収のタスクを、生成から何日たったら起票するか（T33、ADR-0022）
    signed_copy_due_days = models.PositiveSmallIntegerField(
        _("days until the signed copy is collected"), default=14
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
    # 様式で「〇〇語」の前に書く短い名前（例: 中国。第1-17号。T31-D11）
    form_name = models.CharField(_("name on forms"), max_length=20, blank=True)
    sort_order = models.PositiveSmallIntegerField(_("sort order"), default=0)
    is_active = models.BooleanField(_("active"), default=True)

    class Meta:
        verbose_name = _("support language")
        verbose_name_plural = _("support languages")
        ordering = ["sort_order", "code"]

    def __str__(self) -> str:
        return self.name


class Country(TrackedModel):
    """国・地域のマスタ（国籍・旅券の発行国）。帳票で国名を使うため、名称は入管様式の表記に合わせる。"""

    code = models.CharField(_("ISO code"), max_length=2, unique=True)
    name = models.CharField(_("name"), max_length=50)
    sort_order = models.PositiveSmallIntegerField(_("sort order"), default=100)
    is_active = models.BooleanField(_("active"), default=True)

    class Meta:
        verbose_name = _("country or region")
        verbose_name_plural = _("countries and regions")
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
    # 支援計画書の担当者は「氏名（役職）」で書く（T17）
    position = models.CharField(_("position"), max_length=50, blank=True)
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
    WORKERS_VIEW = "workers.view", _("view foreign workers")
    WORKERS_MANAGE = "workers.manage", _("register and update foreign workers")
    WORKERS_DEACTIVATE = "workers.deactivate", _("deactivate foreign workers")
    WORKERS_SENSITIVE_DOCUMENTS = (
        "workers.sensitive_documents",
        _("download sensitive documents (health checks) of all foreign workers"),
    )
    HOSTS_VIEW = "hosts.view", _("view host organizations")
    HOSTS_MANAGE = "hosts.manage", _("register and update host organizations")
    MASTERS_MANAGE = "masters.manage", _("edit master data (countries, fields, categories)")
    CONTRACTS_VIEW = "contracts.view", _("view employment contracts")
    CONTRACTS_MANAGE = "contracts.manage", _("register and update employment contracts")
    CONTRACTS_REOPEN = "contracts.reopen", _("cancel the termination of employment contracts")
    SUPPORT_VIEW = "support.view", _("view support plans and records")
    SUPPORT_MANAGE = "support.manage", _("register support plans and records")
    FILINGS_VIEW = "filings.view", _("view filing tasks")
    FILINGS_MANAGE = "filings.manage", _("register and update filing tasks")
    FORMS_GENERATE = "forms.generate", _("generate and download forms (PDF)")
    SYSTEM_MANAGE = "system.manage", _("manage background jobs")


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


class PasswordReset(TrackedModel):
    """パスワードの再設定のリンク（T25）。招待と同じく、トークンは SHA-256 だけを保存する。

    一度だけ使える。期限は発行から 60 分。新しく発行すると、それより前の未使用のリンクは無効にする。
    """

    user = models.ForeignKey(
        User, verbose_name=_("user"), on_delete=models.PROTECT, related_name="password_resets"
    )
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    expires_at = models.DateTimeField(_("expires at"))
    used_at = models.DateTimeField(_("used at"), null=True, blank=True)
    invalidated_at = models.DateTimeField(_("invalidated at"), null=True, blank=True)
    requested_ip = models.GenericIPAddressField(_("requested from"), null=True, blank=True)

    class Meta:
        verbose_name = _("password reset")
        verbose_name_plural = _("password resets")
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
    WORKER_DEACTIVATE = "worker_deactivate", _("deactivate foreign worker")
    WORKER_REACTIVATE = "worker_reactivate", _("cancel deactivation of foreign worker")
    UNLOCK = "unlock", _("unlock sign-in")
    AUDIT_VIEW = "audit_view", _("view audit logs")
    MFA_RESET = "mfa_reset", _("reset two-factor authentication")
    MFA_ENROLL = "mfa_enroll", _("set up two-factor authentication")
    DOWNLOAD_DENIED = "download_denied", _("download of a sensitive document denied")
    # パスワードの再設定（T25）: 申請の受付（画面）・リンクの送信（ジョブ）・再設定の完了
    PASSWORD_RESET_REQUEST = "password_reset_request", _("password reset requested")
    PASSWORD_RESET_LINK = "password_reset_link", _("password reset link sent")
    PASSWORD_RESET = "password_reset", _("password reset")
    RECOVERY_CODES_REGENERATE = "recovery_codes_regenerate", _("regenerate recovery codes")
    ENCRYPTION_KEY_ROTATION = "encryption_key_rotation", _("rotate encryption keys")
    BLIND_INDEX_REBUILD = "blind_index_rebuild", _("rebuild blind indexes")
    FILING_TRANSITION = "filing_transition", _("change of filing task status")


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
