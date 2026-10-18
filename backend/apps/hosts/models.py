"""受入れ機関（仕様書第6章「対象分野の上乗せ要件」「システムへの反映」、第7章 host_organizations /
host_sites / support_contracts）。

分野・業務区分はマスタ（データ）として持ち、分野の追加はコード変更なしに行う。
登録法人・協議会への加入は受入れ機関×分野で持つ（ADR-0004）。
"""

from datetime import date

from django.conf import settings
from django.contrib.contenttypes.fields import GenericRelation
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateRangeField, RangeBoundary, RangeOperators
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import F, Func, Q
from django.db.models.functions import Least
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
        help_text=_("13 digits. A sole proprietor has none (leave it blank)."),
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
            # 個人事業主は法人番号を持たない（T28a の確認の後の修正）
            models.CheckConstraint(
                condition=~Q(entity_type=EntityType.SOLE_PROPRIETOR) | Q(corporate_number=""),
                name="hosts_hostorganization_sole_proprietor_has_no_number",
                violation_error_message=_(
                    "A sole proprietor has no corporate number. Leave it blank."
                ),
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
    # 雇用条件書（参考様式第1-6号）Ⅱ 就業の場所の「連絡先」（T28b、ADR-0016）
    phone_number = models.CharField(_("phone number"), max_length=20, blank=True)
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


class SupportTerminationCategory(models.TextChoices):
    """支援委託契約の終了の事由の大分類（参考様式第3-3-2号 A b。ADR-0017）。"""

    EXPIRY = "expiry", _("expiry of the entrustment period")
    HOST = "host", _("for reasons of the host organization")
    SUPPORT_ORGANIZATION = (
        "support_organization",
        _("for reasons of the registered support organization"),
    )


class SupportTerminationReason(models.TextChoices):
    """終了の事由の小分類（第3-3-2号の記載要領の対応表）。"""

    EXPIRY = "expiry", _("expiry of the period")
    BUSINESS = "business", _("business reasons")
    BREACH = "breach", _("breach of contract")
    REGISTRATION_REVOKED = "registration_revoked", _("revocation of registration")
    OTHER = "other", _("other")


# 大分類ごとに選べる小分類（記載要領の対応表）
SUPPORT_TERMINATION_REASONS: dict[str, tuple[str, ...]] = {
    SupportTerminationCategory.EXPIRY: (SupportTerminationReason.EXPIRY,),
    SupportTerminationCategory.HOST: (
        SupportTerminationReason.BUSINESS,
        SupportTerminationReason.BREACH,
        SupportTerminationReason.OTHER,
    ),
    SupportTerminationCategory.SUPPORT_ORGANIZATION: (
        SupportTerminationReason.REGISTRATION_REVOKED,
        SupportTerminationReason.BREACH,
        SupportTerminationReason.BUSINESS,
        SupportTerminationReason.OTHER,
    ),
}


class DateRange(Func):
    """PostgreSQL の daterange(開始, 終了, '[]')。終了が NULL なら期限なし。"""

    function = "DATERANGE"
    output_field = DateRangeField()


class SupportContract(TrackedModel):
    """自機関と受入れ機関の支援委託契約。同じ受入れ機関で期間の重なる契約は持てない。

    1 行＝1 期間（ADR-0015）。ends_on は契約の予定の終了日、terminated_on は終了を登録した実際の
    終了日（ADR-0017）。実際の終了日がある行は、それを期間の末日とする（effective_ends_on）。
    """

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
    # 委託料（1 名あたりの月額。第1-25号 第5欄・第5-10号 第2条。内訳〔別紙〕は T37）
    monthly_fee = models.PositiveIntegerField(
        _("entrustment fee per person per month (yen)"), null=True, blank=True
    )
    # 終了の登録（第3-3-2号 A。ADR-0017）
    terminated_on = models.DateField(_("terminated on"), null=True, blank=True)
    termination_category = models.CharField(
        _("termination category"),
        max_length=24,
        choices=SupportTerminationCategory.choices,
        blank=True,
    )
    termination_reason = models.CharField(
        _("termination reason"), max_length=24, choices=SupportTerminationReason.choices, blank=True
    )
    termination_note = models.CharField(
        _("termination reason (other)"),
        max_length=20,
        blank=True,
        help_text=_("Up to 20 characters (written on form 3-3-2)."),
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
            models.CheckConstraint(
                condition=Q(terminated_on__isnull=True)
                | (
                    Q(terminated_on__gte=F("starts_on"))
                    & (Q(ends_on__isnull=True) | Q(terminated_on__lte=F("ends_on")))
                ),
                name="hosts_supportcontract_terminated_within_period",
                violation_error_message=_(
                    "The termination date must be within the contract period."
                ),
            ),
            models.CheckConstraint(
                condition=Q(terminated_on__isnull=True) | ~Q(termination_category=""),
                name="hosts_supportcontract_termination_has_category",
                violation_error_message=_("Select the termination category."),
            ),
            ExclusionConstraint(
                name="hosts_supportcontract_no_overlap",
                expressions=[
                    (
                        # 開始日〜期間の末日（実際の終了日があればそれ、無ければ予定の終了日）。
                        # 両端を含む（同じ日に終わる契約と始まる契約は重なる）
                        DateRange(
                            "starts_on",
                            Least("ends_on", "terminated_on"),
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

    @property
    def effective_ends_on(self) -> date | None:
        """期間の末日（実際の終了日があればそれ、無ければ予定の終了日。空は期限なし）。"""
        return self.terminated_on or self.ends_on


# --- キャリアアッププラン（T36、ADR-0025）-----------------------------------------------------


class CareerPlanImmutableError(Exception):
    """キャリアアッププランの版とレベルは作成後に変更・削除できない。"""


class CareerPlan(TrackedModel):
    """キャリアアッププランの見出し（受入れ機関×分野。事業所ごとに違えば事業所で上書き。T36-D1）。

    飲食料品製造業分野の告示（雇用契約の締結の前に書面を交付して説明）による。様式は任意で、
    内容の例は別冊（飲食料品製造業分野）18頁の 4 項目。工業の契約でも作れる（T36-D8）。
    """

    host_organization = models.ForeignKey(
        HostOrganization,
        verbose_name=_("host organization"),
        on_delete=models.PROTECT,
        related_name="career_plans",
    )
    field = models.ForeignKey(
        SpecifiedSkillField,
        verbose_name=_("specified skill field"),
        on_delete=models.PROTECT,
        related_name="+",
    )
    site = models.ForeignKey(
        HostSite,
        verbose_name=_("site (only when it differs by site)"),
        on_delete=models.PROTECT,
        related_name="career_plans",
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = _("career-up plan")
        verbose_name_plural = _("career-up plans")
        ordering = ["host_organization", "field", "site", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["host_organization", "field"],
                condition=Q(site__isnull=True),
                name="hosts_careerplan_one_per_field",
            ),
            models.UniqueConstraint(
                fields=["host_organization", "field", "site"],
                condition=Q(site__isnull=False),
                name="hosts_careerplan_one_per_site",
            ),
        ]

    def __str__(self) -> str:
        site = f" / {self.site.name}" if self.site is not None else ""
        return f"{self.host_organization} / {self.field}{site}"


class _CareerPlanRow(TrackedModel):
    class Meta:
        abstract = True

    def save(self, *args: object, **kwargs: object) -> None:
        if not self._state.adding:
            raise CareerPlanImmutableError(f"{self._meta.label} rows cannot be changed")
        super().save(*args, **kwargs)  # type: ignore[arg-type]

    def delete(self, *args: object, **kwargs: object) -> tuple[int, dict[str, int]]:
        raise CareerPlanImmutableError(f"{self._meta.label} rows cannot be deleted")


class CareerPlanVersion(_CareerPlanRow):
    """プランの版（追記だけ。交付したものを再現できる）。"""

    plan = models.ForeignKey(
        CareerPlan,
        verbose_name=_("career-up plan"),
        on_delete=models.PROTECT,
        related_name="versions",
    )
    version = models.PositiveIntegerField(_("version"), editable=False)
    applies_from = models.DateField(_("applies from"))
    change_reason = models.TextField(_("change reason"), blank=True)
    career_route = models.TextField(_("expected career route"))
    transfer_scope = models.TextField(
        _("scope of job changes"),
        blank=True,
        help_text=_(
            "For the seafood processing category (from 1 April 2027): the scope within which the "
            "worker can change jobs (in principle the same category), explained for consent."
        ),
    )

    class Meta:
        verbose_name = _("career-up plan version")
        verbose_name_plural = _("career-up plan versions")
        ordering = ["plan", "-version"]
        constraints = [
            models.UniqueConstraint(fields=["plan", "version"], name="hosts_careerplan_version")
        ]

    def __str__(self) -> str:
        return f"{self.plan} v{self.version}"


class CareerPlanLevel(_CareerPlanRow):
    """各レベルの業務・習熟の目安の年数・必要な経験と資格・地位と処遇（別冊 18頁の例）。"""

    version = models.ForeignKey(
        CareerPlanVersion,
        verbose_name=_("career-up plan version"),
        on_delete=models.PROTECT,
        related_name="levels",
    )
    order = models.PositiveSmallIntegerField(_("order"))
    name = models.CharField(_("level"), max_length=50)
    years = models.CharField(
        _("years as a guide"), max_length=50, help_text=_("Example: 1st to 2nd year.")
    )
    duties = models.TextField(_("duties"))
    requirements = models.TextField(_("experience, results, qualifications and tests"), blank=True)
    position = models.CharField(_("position"), max_length=100, blank=True)
    treatment = models.TextField(_("treatment such as wages"), blank=True)

    class Meta:
        verbose_name = _("career-up plan level")
        verbose_name_plural = _("career-up plan levels")
        ordering = ["version", "order", "pk"]

    def __str__(self) -> str:
        return f"{self.version}: {self.name}"
