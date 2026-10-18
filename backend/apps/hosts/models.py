"""受入れ機関（仕様書第6章「対象分野の上乗せ要件」「システムへの反映」、第7章 host_organizations /
host_sites / support_contracts）。

分野・業務区分はマスタ（データ）として持ち、分野の追加はコード変更なしに行う。
登録法人・協議会への加入は受入れ機関×分野で持つ（ADR-0004）。
"""

from django.conf import settings
from django.contrib.contenttypes.fields import GenericRelation
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateRangeField, RangeBoundary, RangeOperators
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import F, Func, Q
from django.utils.translation import gettext_lazy as _

from apps.core.models import Attachment, TrackedModel
from services import corporate_number


class MembershipType(models.TextChoices):
    REGISTERED_CORPORATION = "registered_corporation", _("registered corporation")
    COUNCIL = "council", _("council")


class SpecifiedSkillField(TrackedModel):
    """特定技能の分野（マスタ）。MVP は工業製品製造業分野・飲食料品製造業分野。"""

    code = models.CharField(_("code"), max_length=32, unique=True)
    name = models.CharField(_("name"), max_length=50)
    competent_ministry = models.CharField(_("competent ministry"), max_length=50)
    membership_type = models.CharField(
        _("membership type"), max_length=32, choices=MembershipType.choices
    )
    membership_body_name = models.CharField(_("membership body"), max_length=100)
    support_org_membership_required = models.BooleanField(
        _("registered support organization must also be a member"), default=False
    )
    sort_order = models.PositiveSmallIntegerField(_("sort order"), default=100)
    is_active = models.BooleanField(_("active"), default=True)

    class Meta:
        verbose_name = _("specified skill field")
        verbose_name_plural = _("specified skill fields")
        ordering = ["sort_order", "code"]

    def __str__(self) -> str:
        return self.name


class OperationCategory(TrackedModel):
    """業務区分（マスタ。分野→区分の 2 階層）。適用開始日・終了日で有効な区分を引く。

    開始日が空 = 制度上の開始日を定めない（現行）。終了日が空 = 期限なし。
    """

    field = models.ForeignKey(
        SpecifiedSkillField,
        verbose_name=_("specified skill field"),
        on_delete=models.PROTECT,
        related_name="categories",
    )
    code = models.CharField(_("code"), max_length=32, unique=True)
    name = models.CharField(_("name"), max_length=50)
    valid_from = models.DateField(_("valid from"), null=True, blank=True)
    valid_until = models.DateField(_("valid until"), null=True, blank=True)
    sort_order = models.PositiveSmallIntegerField(_("sort order"), default=100)

    class Meta:
        verbose_name = _("operation category")
        verbose_name_plural = _("operation categories")
        ordering = ["field__sort_order", "sort_order", "code"]
        constraints = [
            models.CheckConstraint(
                condition=Q(valid_from__isnull=True)
                | Q(valid_until__isnull=True)
                | Q(valid_until__gte=F("valid_from")),
                name="hosts_operationcategory_period",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class EntityType(models.TextChoices):
    CORPORATION = "corporation", _("corporation")
    SOLE_PROPRIETOR = "sole_proprietor", _("sole proprietor")


class HostOrganization(TrackedModel):
    """受入れ機関（特定技能所属機関）。将来の受入れ機関ポータルでの範囲の単位。"""

    entity_type = models.CharField(
        _("entity type"), max_length=16, choices=EntityType.choices, default=EntityType.CORPORATION
    )
    name = models.CharField(_("name"), max_length=200)
    name_kana = models.CharField(_("name (kana)"), max_length=200, blank=True)
    corporate_number = models.CharField(
        _("corporate number"),
        max_length=13,
        blank=True,
        validators=[corporate_number.validate],
        help_text=_("13 digits. Leave blank for a sole proprietor."),
    )
    representative_title = models.CharField(_("representative title"), max_length=50, blank=True)
    representative_name = models.CharField(_("representative name"), max_length=100)
    postal_code = models.CharField(_("postal code"), max_length=8)
    address = models.CharField(_("address"), max_length=255)
    phone_number = models.CharField(_("phone number"), max_length=20, blank=True)
    is_active = models.BooleanField(_("active"), default=True)
    # 将来の受入れ機関ポータル用（MVP では空のまま）
    users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        verbose_name=_("portal users"),
        related_name="host_organizations",
        blank=True,
    )

    class Meta:
        verbose_name = _("host organization")
        verbose_name_plural = _("host organizations")
        ordering = ["name", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["corporate_number"],
                condition=~Q(corporate_number=""),
                name="hosts_hostorganization_corporate_number_unique",
                violation_error_message=_("A host organization with this corporate number exists."),
            ),
            models.CheckConstraint(
                condition=Q(entity_type=EntityType.SOLE_PROPRIETOR) | ~Q(corporate_number=""),
                name="hosts_hostorganization_corporation_has_number",
                violation_error_message=_("Enter the corporate number of a corporation."),
            ),
        ]

    def __str__(self) -> str:
        return self.name


class HostMembership(TrackedModel):
    """受入れ機関の登録法人（工業）・協議会（飲食料品）への加入（受入れ機関×分野。ADR-0004）。"""

    host_organization = models.ForeignKey(
        HostOrganization,
        verbose_name=_("host organization"),
        on_delete=models.PROTECT,
        related_name="memberships",
    )
    field = models.ForeignKey(
        SpecifiedSkillField,
        verbose_name=_("specified skill field"),
        on_delete=models.PROTECT,
        related_name="+",
    )
    joined_on = models.DateField(_("joined on"))
    valid_until = models.DateField(_("valid until"), null=True, blank=True)
    member_number = models.CharField(_("member number"), max_length=50, blank=True)
    attachments = GenericRelation(Attachment)

    class Meta:
        verbose_name = _("membership")
        verbose_name_plural = _("memberships")
        ordering = ["host_organization", "field__sort_order"]
        constraints = [
            models.UniqueConstraint(
                fields=["host_organization", "field"], name="hosts_hostmembership_unique"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.host_organization}: {self.field}"


class HostCooperationConfirmation(TrackedModel):
    """協力確認書の提出（1 行＝事業所×提出先の市区町村への 1 回の提出。ADR-0015）。

    提出先は事業所の所在地の市区町村に限らない（外国人の住居地の市区町村への提出も同じ事業所の行で
    持つ）。有効期限は無く、記載事項が変わって出し直したら新しい行を足す（最新の行が今の提出）。
    """

    host_organization = models.ForeignKey(
        HostOrganization,
        verbose_name=_("host organization"),
        on_delete=models.PROTECT,
        related_name="cooperation_confirmations",
    )
    site = models.ForeignKey(
        "HostSite",
        verbose_name=_("host site"),
        on_delete=models.PROTECT,
        related_name="cooperation_confirmations",
    )
    municipality = models.CharField(_("submitted to (municipality)"), max_length=100)
    submitted_on = models.DateField(_("submitted on"))
    attachments = GenericRelation(Attachment)

    class Meta:
        verbose_name = _("cooperation confirmation")
        verbose_name_plural = _("cooperation confirmations")
        ordering = ["host_organization", "site", "municipality", "-submitted_on", "-pk"]

    def __str__(self) -> str:
        return f"{self.host_organization}: {self.municipality}"


class HostSite(TrackedModel):
    """受入れ機関の事業所（雇用契約の就業場所）。"""

    host_organization = models.ForeignKey(
        HostOrganization,
        verbose_name=_("host organization"),
        on_delete=models.PROTECT,
        related_name="sites",
    )
    name = models.CharField(_("name"), max_length=200)
    postal_code = models.CharField(_("postal code"), max_length=8)
    address = models.CharField(_("address"), max_length=255)
    industry_code = models.CharField(
        _("Japan Standard Industrial Classification code"),
        max_length=4,
        validators=[
            RegexValidator(r"^[0-9]{2,4}$", _("Enter 2 to 4 digits (e.g. 09, 0991, 2194)."))
        ],
    )
    field = models.ForeignKey(
        SpecifiedSkillField,
        verbose_name=_("specified skill field"),
        on_delete=models.PROTECT,
        related_name="sites",
    )
    categories = models.ManyToManyField(
        OperationCategory, verbose_name=_("operation categories"), related_name="sites"
    )
    is_active = models.BooleanField(_("active"), default=True)

    class Meta:
        verbose_name = _("host site")
        verbose_name_plural = _("host sites")
        ordering = ["host_organization", "name"]

    def __str__(self) -> str:
        return f"{self.host_organization} {self.name}"


class SupportScope(models.TextChoices):
    FULL = "full", _("full entrustment")


class DateRange(Func):
    """PostgreSQL の daterange(開始, 終了, '[]')。終了が NULL なら期限なし。"""

    function = "DATERANGE"
    output_field = DateRangeField()


class SupportContract(TrackedModel):
    """自機関と受入れ機関の支援委託契約。同じ受入れ機関で期間の重なる契約は持てない。"""

    host_organization = models.ForeignKey(
        HostOrganization,
        verbose_name=_("host organization"),
        on_delete=models.PROTECT,
        related_name="support_contracts",
    )
    concluded_on = models.DateField(_("concluded on"))
    starts_on = models.DateField(_("starts on"))
    ends_on = models.DateField(_("ends on"), null=True, blank=True)
    scope = models.CharField(
        _("scope"), max_length=16, choices=SupportScope.choices, default=SupportScope.FULL
    )
    attachments = GenericRelation(Attachment)

    class Meta:
        verbose_name = _("support contract")
        verbose_name_plural = _("support contracts")
        ordering = ["host_organization", "-starts_on"]
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_on__isnull=True) | Q(ends_on__gte=F("starts_on")),
                name="hosts_supportcontract_period",
                violation_error_message=_("The end date must be on or after the start date."),
            ),
            ExclusionConstraint(
                name="hosts_supportcontract_no_overlap",
                expressions=[
                    (
                        # 開始日・終了日とも含む（同じ日に終わる契約と始まる契約は重なる）
                        DateRange(
                            "starts_on",
                            "ends_on",
                            RangeBoundary(inclusive_lower=True, inclusive_upper=True),
                        ),
                        RangeOperators.OVERLAPS,
                    ),
                    ("host_organization", RangeOperators.EQUAL),
                ],
                violation_error_message=_(
                    "This period overlaps another support contract of the same host organization."
                ),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.host_organization}: {self.starts_on}~{self.ends_on or ''}"
