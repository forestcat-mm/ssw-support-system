"""自機関側のマスタ（仕様書第7章 organization / users / offices / staff / attachments）。"""

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from services.files import attachment_upload_to, logo_upload_to, validate_logo


class User(AbstractUser):
    """ログインユーザー。ロール・MFA・招待は T04 で追加する。"""

    class Meta:
        verbose_name = _("user")
        verbose_name_plural = _("users")


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
