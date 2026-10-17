"""支援計画（第5章 F04、第7章 support_plans / support_plan_items、T16）。

- 見出し `SupportPlan` は外国人×受入れ機関（転職などで受入れ機関が変われば新しい見出し）。
  自機関の標準の計画は、外国人・受入れ機関を持たない見出し（`is_template`）として同じ形で持つ。
- 版 `SupportPlanVersion` と、その中身（10項目・細目・住居の概要・相談の受付・共生施策の確認）は
  作成後に変更・削除しない。訂正も新しい版で行う（段階3で、どの版から帳票を作ったかを追えるように）。
- 細目の行は参考様式第1-17号から逆算した services.support_catalog.ROWS に対応する（ADR-0006）。
"""

from typing import Any

from django.contrib.contenttypes.fields import GenericRelation
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.core.models import Attachment, Staff, SupportLanguage, TrackedModel
from apps.filings.models import Filing
from apps.hosts.models import HostOrganization
from apps.workers.models import ForeignWorker
from services.support_catalog import Method, SupportItem


class ImmutableRowError(Exception):
    """支援計画の版（とその中身）は作成後に変更・削除できない。"""


class ImmutableRow(TrackedModel):
    """作成後に変更・削除しない行の共通部分。"""

    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise ImmutableRowError(f"{self._meta.label} rows cannot be changed")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ImmutableRowError(f"{self._meta.label} rows cannot be deleted")


class SupportPlan(TrackedModel):
    """支援計画の見出し（外国人×受入れ機関）。標準の計画は is_template で 1 つだけ。"""

    worker = models.ForeignKey(
        ForeignWorker,
        verbose_name=_("foreign worker"),
        on_delete=models.PROTECT,
        related_name="support_plans",
        null=True,
        blank=True,
    )
    host_organization = models.ForeignKey(
        HostOrganization,
        verbose_name=_("host organization"),
        on_delete=models.PROTECT,
        related_name="support_plans",
        null=True,
        blank=True,
    )
    is_template = models.BooleanField(_("standard plan"), default=False, editable=False)

    class Meta:
        verbose_name = _("support plan")
        verbose_name_plural = _("support plans")
        constraints = [
            models.CheckConstraint(
                condition=Q(is_template=True, worker__isnull=True, host_organization__isnull=True)
                | Q(is_template=False, worker__isnull=False, host_organization__isnull=False),
                name="support_plan_template_or_worker",
            ),
            models.UniqueConstraint(
                fields=["worker", "host_organization"],
                condition=Q(is_template=False),
                name="support_plan_one_per_worker_and_host",
            ),
            models.UniqueConstraint(
                fields=["is_template"],
                condition=Q(is_template=True),
                name="support_plan_one_template",
            ),
        ]

    def __str__(self) -> str:
        if self.is_template:
            return str(_("standard support plan"))
        return f"{self.worker} / {self.host_organization}"


class InterviewPreference(models.TextChoices):
    ONLINE = "online", _("online")
    IN_PERSON = "in_person", _("in person")


class SupportPlanVersion(ImmutableRow):
    """支援計画の版。作成後は変更しない（訂正も新しい版）。"""

    plan = models.ForeignKey(
        SupportPlan,
        verbose_name=_("support plan"),
        on_delete=models.PROTECT,
        related_name="versions",
    )
    version = models.PositiveIntegerField(_("version"), editable=False)
    applies_from = models.DateField(_("applies from"))
    change_reason = models.TextField(_("change reason"), blank=True)
    # 計画書の欄（参考様式第1-17号）
    created_on = models.DateField(_("prepared on"), null=True, blank=True)
    responsible_name = models.CharField(
        _("name of the person responsible for preparation"), max_length=100, blank=True
    )
    translation_language = models.ForeignKey(
        SupportLanguage,
        verbose_name=_("language of the translation given to the worker"),
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
    )
    interview_preference = models.CharField(
        _("worker's preference for periodic interviews"),
        max_length=16,
        choices=InterviewPreference.choices,
        blank=True,
    )

    class Meta:
        verbose_name = _("support plan version")
        verbose_name_plural = _("support plan versions")
        ordering = ["plan", "-version"]
        constraints = [
            models.UniqueConstraint(fields=["plan", "version"], name="support_plan_version_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.plan} v{self.version}"


class SupportPlanItem(ImmutableRow):
    """版×義務的支援10項目で 1 行（区分ごとの欄: 実施言語・通訳者・実施予定時間）。"""

    version = models.ForeignKey(
        SupportPlanVersion,
        verbose_name=_("version"),
        on_delete=models.PROTECT,
        related_name="items",
    )
    item = models.CharField(_("support item"), max_length=32, choices=SupportItem.choices)
    languages = models.ManyToManyField(
        SupportLanguage, verbose_name=_("languages"), related_name="+", blank=True
    )
    interpreter = models.CharField(
        _("interpreter (organization and name)"),
        max_length=200,
        blank=True,
        help_text=_("Only when someone other than the support staff interprets."),
    )
    planned_hours = models.DecimalField(
        _("planned hours in total"), max_digits=5, decimal_places=1, null=True, blank=True
    )

    class Meta:
        verbose_name = _("support plan item")
        verbose_name_plural = _("support plan items")
        constraints = [
            models.UniqueConstraint(fields=["version", "item"], name="support_plan_item_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.version}: {self.get_item_display()}"


class SupportPlanActivity(ImmutableRow):
    """細目の行（実施予定・委託の有無・担当者・実施方法）。符号は services.support_catalog.ROWS。"""

    version = models.ForeignKey(
        SupportPlanVersion,
        verbose_name=_("version"),
        on_delete=models.PROTECT,
        related_name="activities",
    )
    code = models.CharField(_("row"), max_length=16)
    content = models.TextField(
        _("support content"), blank=True, help_text=_("Only for the free-entry rows.")
    )
    planned = models.BooleanField(_("planned"))
    planned_note = models.CharField(
        _("timing, or reason for not implementing"), max_length=200, blank=True
    )
    outsourced = models.BooleanField(
        _("outsourced to a third party"),
        default=False,
        help_text=_(
            "Only when the host organization outsources part of the support to a third party "
            "other than the registered support organization."
        ),
    )
    staff = models.ForeignKey(
        Staff,
        verbose_name=_("support staff in charge"),
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
    )
    outsourcee_name = models.CharField(_("name of the contractor"), max_length=200, blank=True)
    outsourcee_title = models.CharField(_("title of the contractor"), max_length=100, blank=True)
    outsourcee_address = models.CharField(
        _("address of the contractor"), max_length=255, blank=True
    )
    methods = ArrayField(
        models.CharField(max_length=32, choices=Method.choices),
        verbose_name=_("methods"),
        default=list,
        blank=True,
    )
    method_notes = models.JSONField(
        _("notes on methods"),
        default=dict,
        blank=True,
        help_text=_("Text for “other”, airports and means of transfer."),
    )
    method_text = models.TextField(
        _("method (text)"), blank=True, help_text=_("For rows without check boxes.")
    )
    note = models.TextField(
        _("notes when different for each content"),
        blank=True,
    )

    class Meta:
        verbose_name = _("support plan activity")
        verbose_name_plural = _("support plan activities")
        ordering = ["version", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["version", "code"], name="support_plan_activity_unique"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.version}: {self.code}"


class HousingTiming(models.TextChoices):
    SECURED = "secured", _("secured at the time of the application")
    AFTER = "after", _("to be secured after the application")


class PropertyType(models.TextChoices):
    OWNED = "owned", _("owned property")
    LEASED = "leased", _("leased property")


class SupportPlanHousing(ImmutableRow):
    """住居の概要（参考様式第1-17号 Ⅳ欄 3アｄ）。版と 1 対 1。"""

    version = models.OneToOneField(
        SupportPlanVersion,
        verbose_name=_("version"),
        on_delete=models.PROTECT,
        related_name="housing",
    )
    timing = models.CharField(
        _("when secured"), max_length=16, choices=HousingTiming.choices, blank=True
    )
    room_area_ok = models.BooleanField(_("room area: 7.5 m² or more per person"), default=False)
    occupants = models.PositiveSmallIntegerField(_("number of occupants"), null=True, blank=True)
    bedroom_area_ok = models.BooleanField(
        _("bedroom area: 4.5 m² or more per person"), default=False
    )
    rent_collected = models.BooleanField(_("housing costs collected"), null=True, blank=True)
    monthly_rent = models.PositiveIntegerField(
        _("housing costs per month (yen)"), null=True, blank=True
    )
    property_type = models.CharField(
        _("type of accommodation"), max_length=16, choices=PropertyType.choices, blank=True
    )
    rent_explanation = models.TextField(_("explanation that the cost is appropriate"), blank=True)

    class Meta:
        verbose_name = _("housing outline")
        verbose_name_plural = _("housing outlines")

    def __str__(self) -> str:
        return f"{self.version}: housing"


class ContactMethod(models.TextChoices):
    IN_PERSON = "in_person", _("in person")
    PHONE = "phone", _("phone")
    EMAIL = "email", _("email")
    OTHER = "other", _("other")


class SupportPlanConsultation(ImmutableRow):
    """相談・苦情の受付（参考様式第1-17号 Ⅳ欄 6イ）。版と 1 対 1。

    hours は {"mon": ["09:00", "18:00"], ..., "sat": [...], "sun": [...], "holiday": [...]}。
    """

    version = models.OneToOneField(
        SupportPlanVersion,
        verbose_name=_("version"),
        on_delete=models.PROTECT,
        related_name="consultation",
    )
    hours = models.JSONField(_("hours"), default=dict, blank=True)
    methods = ArrayField(
        models.CharField(max_length=16, choices=ContactMethod.choices),
        verbose_name=_("consultation methods"),
        default=list,
        blank=True,
    )
    phone = models.CharField(_("phone number"), max_length=30, blank=True)
    email = models.EmailField(_("email"), blank=True)
    other = models.CharField(_("other method"), max_length=200, blank=True)
    emergency_methods = ArrayField(
        models.CharField(max_length=16, choices=ContactMethod.choices),
        verbose_name=_("methods in an emergency"),
        default=list,
        blank=True,
    )
    emergency_phone = models.CharField(_("phone number in an emergency"), max_length=30, blank=True)
    emergency_email = models.EmailField(_("email in an emergency"), blank=True)
    emergency_other = models.CharField(
        _("other method in an emergency"), max_length=200, blank=True
    )

    class Meta:
        verbose_name = _("consultation desk")
        verbose_name_plural = _("consultation desks")

    def __str__(self) -> str:
        return f"{self.version}: consultation"


class MunicipalityPlace(models.TextChoices):
    SITE = "site", _("location of the workplace")
    RESIDENCE = "residence", _("residence of the worker")


class CheckMethod(models.TextChoices):
    WEBSITE = "website", _("website")
    OTHER = "other", _("other method")


class SupportPlanMunicipalityCheck(ImmutableRow):
    """共生施策の確認（参考様式第1-17号 Ⅴ欄）。事業所の所在地と住居地の 2 行まで。"""

    version = models.ForeignKey(
        SupportPlanVersion,
        verbose_name=_("version"),
        on_delete=models.PROTECT,
        related_name="municipality_checks",
    )
    place = models.CharField(_("place"), max_length=16, choices=MunicipalityPlace.choices)
    same_as_site = models.BooleanField(
        _("same as the workplace"), default=False, help_text=_("Only for the residence.")
    )
    prefecture = models.CharField(_("prefecture"), max_length=10, blank=True)
    municipality = models.CharField(_("municipality"), max_length=50, blank=True)
    checked_on = models.DateField(_("checked on"), null=True, blank=True)
    method = models.CharField(
        _("how checked"), max_length=16, choices=CheckMethod.choices, blank=True
    )
    method_note = models.CharField(_("other method (details)"), max_length=200, blank=True)
    note = models.CharField(_("remarks"), max_length=200, blank=True)

    class Meta:
        verbose_name = _("check of coexistence measures")
        verbose_name_plural = _("checks of coexistence measures")
        constraints = [
            models.UniqueConstraint(
                fields=["version", "place"], name="support_plan_municipality_unique"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.version}: {self.get_place_display()}"


# --- 支援実績（T18）-------------------------------------------------------------------


class RecordMethod(models.TextChoices):
    IN_PERSON = "in_person", _("in person")
    ONLINE = "online", _("online")
    PHONE = "phone", _("phone")
    VIDEO = "video", _("watching videos (DVD, etc.)")  # 生活オリエンテーションだけ（T21）
    MESSAGE = "message", _("message (email, social media, etc.)")  # 相談・苦情だけ（T20）


class Counterpart(models.TextChoices):
    WORKER = "worker", _("the worker")
    SUPERVISOR = "supervisor", _("supervisor at the host organization")


class BodyLanguage(models.TextChoices):
    """本文の言語（表示の lang 属性に使う）。"""

    JA = "ja", _("Japanese")
    ZH_HANS = "zh-Hans", _("Chinese (Simplified)")


class SupportRecord(TrackedModel):
    """支援の実施記録（仕様書 第7章 support_records）。上書きせず、誤りは取消＋新しい行。

    種別ごとの最小限の欄は details に持ち、形は services.support_records.DETAIL_FIELDS で検査する。
    定期面談・相談・ガイダンスの欄は T19〜T21 で専用の表を 1 対 1 で足す。
    """

    worker = models.ForeignKey(
        ForeignWorker,
        verbose_name=_("foreign worker"),
        on_delete=models.PROTECT,
        related_name="support_records",
    )
    kind = models.CharField(_("kind of support"), max_length=32, choices=SupportItem.choices)
    occurred_on = models.DateField(_("date of support"))
    start_time = models.TimeField(_("time started"), null=True, blank=True)
    duration_minutes = models.PositiveIntegerField(_("duration (minutes)"), null=True, blank=True)
    method = models.CharField(_("method"), max_length=16, choices=RecordMethod.choices)
    meeting_url = models.URLField(_("online meeting URL"), max_length=500, blank=True)
    staff = models.ForeignKey(
        Staff, verbose_name=_("staff in charge"), on_delete=models.PROTECT, related_name="+"
    )
    counterpart = models.CharField(
        _("counterpart"), max_length=16, choices=Counterpart.choices, default=Counterpart.WORKER
    )
    counterpart_name = models.CharField(_("name of the supervisor"), max_length=100, blank=True)
    languages = models.ManyToManyField(
        SupportLanguage, verbose_name=_("languages used"), related_name="+", blank=True
    )
    body = models.TextField(_("details"), blank=True)
    body_language = models.CharField(
        _("language of the details"),
        max_length=8,
        choices=BodyLanguage.choices,
        default=BodyLanguage.JA,
    )
    details = models.JSONField(_("details by kind"), default=dict, blank=True)
    plan_version = models.ForeignKey(
        SupportPlanVersion,
        verbose_name=_("support plan version"),
        on_delete=models.PROTECT,
        related_name="records",
        null=True,
        blank=True,
        help_text=_("Set automatically from the date: the plan of the host organization then."),
    )
    voided_at = models.DateTimeField(_("voided at"), null=True, blank=True, editable=False)
    voided_by = models.ForeignKey(
        "core.User",
        verbose_name=_("voided by"),
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
        editable=False,
    )
    void_reason = models.CharField(_("void reason"), max_length=200, blank=True, editable=False)
    interview = models.ForeignKey(
        "PeriodicInterview",
        verbose_name=_("periodic interview"),
        on_delete=models.PROTECT,
        related_name="records",
        null=True,
        blank=True,
        editable=False,
        help_text=_("The pair of rows (worker and supervisor) of a periodic interview."),
    )
    attachments = GenericRelation(Attachment)

    class Meta:
        verbose_name = _("support record")
        verbose_name_plural = _("support records")
        ordering = ["-occurred_on", "-start_time", "-pk"]
        indexes = [models.Index(fields=["worker", "kind", "occurred_on"])]

    def __str__(self) -> str:
        return f"{self.worker}: {self.get_kind_display()} ({self.occurred_on})"


# --- 定期面談（T19）---------------------------------------------------------------------


class ReportDestination(models.TextChoices):
    LABOR_STANDARDS_OFFICE = "labor_standards_office", _("labour standards inspection office")
    IMMIGRATION = "immigration", _("Immigration Services Agency")
    OTHER = "other", _("other")


class PeriodicInterview(TrackedModel):
    """定期面談の組（本人用・上司用の支援実績 2 行をまとめる）。訂正は組ごとの取消＋新しい組。

    起算日（basis_on）は 2 行の実施日の早い方で、次回の定期面談のタスクの起算日になる。
    """

    worker = models.ForeignKey(
        ForeignWorker,
        verbose_name=_("foreign worker"),
        on_delete=models.PROTECT,
        related_name="periodic_interviews",
    )
    basis_on = models.DateField(_("interview date (basis of the next one)"), editable=False)
    violation_suspected = models.BooleanField(
        _("suspected violation of labour laws"), default=False
    )
    report_required = models.BooleanField(_("report required"), null=True, blank=True)
    report_to = models.CharField(
        _("report to"), max_length=32, choices=ReportDestination.choices, blank=True
    )
    report_to_note = models.CharField(_("report to (details)"), max_length=200, blank=True)
    reported_on = models.DateField(_("reported on"), null=True, blank=True)
    next_scheduled_on = models.DateField(_("next interview scheduled on"), null=True, blank=True)
    completed_filing = models.ForeignKey(
        Filing,
        verbose_name=_("task completed by this interview"),
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
        editable=False,
    )
    next_filing = models.ForeignKey(
        Filing,
        verbose_name=_("task for the next interview"),
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
        editable=False,
    )
    voided_at = models.DateTimeField(_("voided at"), null=True, blank=True, editable=False)
    voided_by = models.ForeignKey(
        "core.User",
        verbose_name=_("voided by"),
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
        editable=False,
    )
    void_reason = models.CharField(_("void reason"), max_length=200, blank=True, editable=False)

    class Meta:
        verbose_name = _("periodic interview")
        verbose_name_plural = _("periodic interviews")
        ordering = ["-basis_on", "-pk"]

    def __str__(self) -> str:
        return f"{self.worker}: {self.basis_on}"


# --- 相談・苦情対応（T20）-----------------------------------------------------------------


class ConsultationCategory(models.TextChoices):
    """主な分類（定期届出の分類。複数にまたがる場合は主なもの 1 つ）。"""

    EMPLOYMENT = "employment", _("work")
    DAILY_LIFE = "daily_life", _("daily life")
    RELATIONSHIPS = "relationships", _("relationships")
    WAGES = "wages", _("wages")
    HEALTH = "health", _("health")
    OTHER = "other", _("other")


class ConsultationStatus(models.TextChoices):
    RECEIVED = "received", _("received")
    IN_PROGRESS = "in_progress", _("in progress")
    RESOLVED = "resolved", _("resolved")
    HANDED_OVER = "handed_over", _("handed over to another organization")


OPEN_CONSULTATION_STATUSES = (ConsultationStatus.RECEIVED, ConsultationStatus.IN_PROGRESS)


class Consultation(TrackedModel):
    """相談・苦情の案件。受付は支援実績の 1 行（種別「相談・苦情対応」）と 1 対 1。

    対応状況・引継ぎ先・通報とハローワーク相談の有無は、経過（ConsultationEntry）の追記でだけ変わる
    （案件の値は経過から写したもの）。訂正は「訂正」の経過を足す。案件ごとの取消だけができる。
    """

    record = models.OneToOneField(
        SupportRecord,
        verbose_name=_("support record (reception)"),
        on_delete=models.PROTECT,
        related_name="consultation",
        editable=False,
    )
    worker = models.ForeignKey(
        ForeignWorker,
        verbose_name=_("foreign worker"),
        on_delete=models.PROTECT,
        related_name="consultations",
        editable=False,
    )
    category = models.CharField(
        _("main category"), max_length=16, choices=ConsultationCategory.choices
    )
    category_note = models.CharField(_("category (details)"), max_length=100, blank=True)
    status = models.CharField(
        _("consultation status"),
        max_length=16,
        choices=ConsultationStatus.choices,
        default=ConsultationStatus.RECEIVED,
        editable=False,
    )
    initial_status = models.CharField(
        _("status at reception"),
        max_length=16,
        choices=ConsultationStatus.choices,
        default=ConsultationStatus.RECEIVED,
        editable=False,
    )
    handed_over_to = models.CharField(
        _("handed over to"), max_length=200, blank=True, editable=False
    )
    reported = models.BooleanField(_("reported to authorities"), default=False, editable=False)
    hello_work = models.BooleanField(_("consulted Hello Work"), default=False, editable=False)
    voided_at = models.DateTimeField(_("voided at"), null=True, blank=True, editable=False)
    voided_by = models.ForeignKey(
        "core.User",
        verbose_name=_("voided by"),
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
        editable=False,
    )
    void_reason = models.CharField(_("void reason"), max_length=200, blank=True, editable=False)

    class Meta:
        verbose_name = _("consultation")
        verbose_name_plural = _("consultations")
        ordering = ["-pk"]
        indexes = [models.Index(fields=["status", "category"])]

    def __str__(self) -> str:
        return f"{self.worker}: {self.get_category_display()} ({self.record.occurred_on})"


class ConsultationEntry(ImmutableRow):
    """対応の経過（追記専用。変更・削除しない）。追記のたびに状態を変えられる。"""

    consultation = models.ForeignKey(
        Consultation,
        verbose_name=_("consultation"),
        on_delete=models.PROTECT,
        related_name="entries",
    )
    occurred_on = models.DateField(_("date of support"))
    start_time = models.TimeField(_("time started"), null=True, blank=True)
    method = models.CharField(_("method"), max_length=16, choices=RecordMethod.choices)
    staff = models.ForeignKey(
        Staff, verbose_name=_("staff in charge"), on_delete=models.PROTECT, related_name="+"
    )
    body = models.TextField(_("details"))
    body_language = models.CharField(
        _("language of the details"),
        max_length=8,
        choices=BodyLanguage.choices,
        default=BodyLanguage.JA,
    )
    status_after = models.CharField(
        _("status after this entry"), max_length=16, choices=ConsultationStatus.choices
    )
    handed_over_to = models.CharField(_("handed over to"), max_length=200, blank=True)
    reported = models.BooleanField(_("reported to authorities"), default=False)
    report_to = models.CharField(
        _("report to"), max_length=32, choices=ReportDestination.choices, blank=True
    )
    report_to_note = models.CharField(_("report to (details)"), max_length=200, blank=True)
    reported_on = models.DateField(_("reported on"), null=True, blank=True)
    hello_work = models.BooleanField(_("consulted Hello Work"), default=False)
    hello_work_on = models.DateField(_("consulted Hello Work on"), null=True, blank=True)

    class Meta:
        verbose_name = _("consultation entry")
        verbose_name_plural = _("consultation entries")
        ordering = ["occurred_on", "start_time", "pk"]

    def __str__(self) -> str:
        return f"{self.consultation_id}: {self.occurred_on} {self.get_status_after_display()}"


# --- 事前ガイダンス・生活オリエンテーション（T21、ADR-0009）----------------------------------


class DepositContract(models.TextChoices):
    """保証金の徴収・財産の管理・違約金等の契約（本人の申告。申請書の「有・無」）。"""

    NONE = "none", _("none")
    EXISTS = "exists", _("exists")


class ForeignFees(models.TextChoices):
    """外国の機関（取次ぎ・活動の準備）への費用の支払。"""

    NONE = "none", _("not paid")
    PAID = "paid", _("paid")


class CheckStatus(models.TextChoices):
    """チェックリストの細目の状態（どちらも付いていない細目は「未チェック」）。"""

    DONE = "done", _("explained")
    NOT_APPLICABLE = "not_applicable", _("not applicable")


class Briefing(TrackedModel):
    """事前ガイダンス・生活オリエンテーション。支援実績の 1 行（種別 1 か 4）と 1 対 1。

    実施日時の行・細目のチェック・外国の機関への費用の行を子に持つ（作成後は変更しない）。
    後から変わるのは署名日だけ（署名済みの確認書を添付したとき）。取消は支援実績の行で行う。
    チェックリストの文言と時間の基準は services/data/briefing_checklists.json の版
    （checklist_version）。
    """

    record = models.OneToOneField(
        SupportRecord,
        verbose_name=_("support record"),
        on_delete=models.PROTECT,
        related_name="briefing",
        editable=False,
    )
    worker = models.ForeignKey(
        ForeignWorker,
        verbose_name=_("foreign worker"),
        on_delete=models.PROTECT,
        related_name="briefings",
        editable=False,
    )
    checklist_version = models.CharField(_("checklist version"), max_length=32, editable=False)
    place = models.CharField(_("place"), max_length=200)
    interpreter = models.CharField(
        _("interpreter (name and affiliation)"), max_length=200, blank=True
    )
    legal_info_provider = models.CharField(
        _("explained legal protection (name and affiliation)"),
        max_length=200,
        blank=True,
        help_text=_("Only when someone other than the staff in charge explained it."),
    )
    continuing_employment = models.BooleanField(
        _("continuing at the same organization without a change in living environment"),
        default=False,
    )
    identity_confirmed = models.BooleanField(_("identity confirmed online"), default=False)
    deposit_contract = models.CharField(
        _("contract on a deposit or penalty"),
        max_length=8,
        choices=DepositContract.choices,
        blank=True,
    )
    deposit_collector = models.CharField(
        _("organization collecting the deposit or managing property"), max_length=200, blank=True
    )
    deposit_detail = models.CharField(
        _("deposit amount or property managed"), max_length=200, blank=True
    )
    foreign_fees = models.CharField(
        _("fees paid to organizations abroad"),
        max_length=8,
        choices=ForeignFees.choices,
        blank=True,
    )
    fees_agreed = models.BooleanField(
        _("understood and agreed on the amount and breakdown"), null=True, blank=True
    )
    signed_on = models.DateField(_("date of signature"), null=True, blank=True)

    class Meta:
        verbose_name = _("guidance or orientation")
        verbose_name_plural = _("guidance and orientation")
        ordering = ["-pk"]

    def __str__(self) -> str:
        return f"{self.worker}: {self.record.get_kind_display()} ({self.record.occurred_on})"


class BriefingSession(ImmutableRow):
    """実施日時の 1 行（確認書の「年月日 時分から時分まで」）。行数に上限は無い。"""

    briefing = models.ForeignKey(
        Briefing,
        verbose_name=_("guidance or orientation"),
        on_delete=models.PROTECT,
        related_name="sessions",
    )
    occurred_on = models.DateField(_("date"))
    start_time = models.TimeField(_("starts at"))
    end_time = models.TimeField(_("ends at"))

    class Meta:
        verbose_name = _("date and time of guidance or orientation")
        verbose_name_plural = _("dates and times of guidance or orientation")
        ordering = ["occurred_on", "start_time", "pk"]

    def __str__(self) -> str:
        return f"{self.occurred_on} {self.start_time:%H:%M}-{self.end_time:%H:%M}"

    @property
    def minutes(self) -> int:
        return (self.end_time.hour * 60 + self.end_time.minute) - (
            self.start_time.hour * 60 + self.start_time.minute
        )


class BriefingCheck(ImmutableRow):
    """チェックリストの細目 1 つ（「説明した」か「該当なし」。該当なしは理由が必須）。"""

    briefing = models.ForeignKey(
        Briefing,
        verbose_name=_("guidance or orientation"),
        on_delete=models.PROTECT,
        related_name="checks",
    )
    code = models.CharField(_("item"), max_length=16)
    status = models.CharField(_("status"), max_length=16, choices=CheckStatus.choices)
    reason = models.CharField(_("reason for not applicable"), max_length=200, blank=True)

    class Meta:
        verbose_name = _("checklist item")
        verbose_name_plural = _("checklist items")
        ordering = ["pk"]
        constraints = [
            models.UniqueConstraint(fields=["briefing", "code"], name="briefing_check_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.code}: {self.status}"


class BriefingForeignFee(ImmutableRow):
    """外国の機関に支払った費用の 1 行（事前ガイダンスで確認する。申請書の欄の元。ADR-0009）。"""

    briefing = models.ForeignKey(
        Briefing,
        verbose_name=_("guidance or orientation"),
        on_delete=models.PROTECT,
        related_name="fees",
    )
    organization_name = models.CharField(_("name of the organization abroad"), max_length=200)
    paid_on = models.DateField(_("paid on"))
    amount_jpy = models.PositiveIntegerField(_("amount (converted into yen)"))
    breakdown = models.CharField(_("breakdown"), max_length=500)

    class Meta:
        verbose_name = _("fee paid to an organization abroad")
        verbose_name_plural = _("fees paid to organizations abroad")
        ordering = ["paid_on", "pk"]

    def __str__(self) -> str:
        return f"{self.organization_name}: {self.amount_jpy}"


# --- 交付記録（T22、ADR-0010）-------------------------------------------------------------


class DeliveryKind(models.TextChoices):
    """本人に交付した書類の種別。"""

    CAREER_UP_PLAN = "career_up_plan", _("career-up plan")
    EXPERIENCE_CERTIFICATE = "experience_certificate", _("certificate of work experience")
    SUPPORT_PLAN_COPY = "support_plan_copy", _("copy of the support plan")
    EMPLOYMENT_CONTRACT_COPY = (
        "employment_contract_copy",
        _("copy of the employment contract and conditions"),
    )
    OTHER = "other", _("other document")


class DeliveryMethod(models.TextChoices):
    """交付方法（告示の「書面を交付し、又は電磁的記録を提供し」）。"""

    HAND = "hand", _("handed over (paper)")
    MAIL = "mail", _("sent by post (paper)")
    ELECTRONIC = "electronic", _("provided electronically (email, etc.)")
    OTHER = "other", _("other")


class DeliveredBy(models.TextChoices):
    HOST = "host", _("host organization")
    ORGANIZATION = "organization", _("our organization")


class DocumentDelivery(TrackedModel):
    """本人に書類を交付した記録。上書きせず、誤りは取消（理由必須）＋新しい行。

    署名済みの控え・受領の記録は添付（種別 delivered_document）。後から変わるのは取消と添付だけ。
    """

    worker = models.ForeignKey(
        ForeignWorker,
        verbose_name=_("foreign worker"),
        on_delete=models.PROTECT,
        related_name="document_deliveries",
    )
    kind = models.CharField(_("document"), max_length=32, choices=DeliveryKind.choices)
    title = models.CharField(
        _("name of the document"),
        max_length=200,
        blank=True,
        help_text=_('Required for "other document".'),
    )
    delivered_on = models.DateField(_("delivered on"))
    method = models.CharField(_("how delivered"), max_length=16, choices=DeliveryMethod.choices)
    method_note = models.CharField(_("how delivered (details)"), max_length=200, blank=True)
    delivered_by = models.CharField(_("delivered by"), max_length=16, choices=DeliveredBy.choices)
    delivered_by_name = models.CharField(
        _("name of the person who delivered it"), max_length=100, blank=True
    )
    explained = models.BooleanField(_("explained the content"), default=False)
    contract = models.ForeignKey(
        "contracts.EmploymentContract",
        verbose_name=_("employment contract"),
        on_delete=models.PROTECT,
        related_name="document_deliveries",
        null=True,
        blank=True,
    )
    requested_on = models.DateField(_("requested on"), null=True, blank=True)
    plan_version = models.ForeignKey(
        SupportPlanVersion,
        verbose_name=_("support plan version"),
        on_delete=models.PROTECT,
        related_name="deliveries",
        null=True,
        blank=True,
        editable=False,
    )
    languages = models.ManyToManyField(
        SupportLanguage, verbose_name=_("languages of the document"), related_name="+", blank=True
    )
    note = models.TextField(_("notes"), blank=True)
    voided_at = models.DateTimeField(_("voided at"), null=True, blank=True, editable=False)
    voided_by = models.ForeignKey(
        "core.User",
        verbose_name=_("voided by"),
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
        editable=False,
    )
    void_reason = models.CharField(_("void reason"), max_length=200, blank=True, editable=False)
    attachments = GenericRelation(Attachment)

    class Meta:
        verbose_name = _("document delivery")
        verbose_name_plural = _("document deliveries")
        ordering = ["-delivered_on", "-pk"]
        indexes = [models.Index(fields=["worker", "kind"])]

    def __str__(self) -> str:
        return f"{self.worker}: {self.get_kind_display()} ({self.delivered_on})"
