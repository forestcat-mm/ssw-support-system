"""雇用契約（仕様書第7章 employment_contracts、第5章 F03）。

契約の見出し（外国人×受入れ機関、終了の登録）と、変更ごとの版に分ける。版は作成後に変更しない
（変更は新しい版）。版の項目は入管の参考様式第1-6号「雇用条件書」（別紙1「賃金の支払」を含む）
から逆算しており、段階3でこの様式を生成する元データになる。
"""

from typing import Any

from django.contrib.contenttypes.fields import GenericRelation
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import RangeBoundary, RangeOperators
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Least
from django.utils.translation import gettext_lazy as _

from apps.core.fields import ChoiceArrayField
from apps.core.models import Attachment, TrackedModel
from apps.hosts.models import DateRange, HostOrganization, HostSite, OperationCategory
from apps.workers.models import ForeignWorker, Sex


class ImmutableRowError(Exception):
    """雇用契約の版（とその明細）は作成後に変更・削除できない。"""


class TerminationReason(models.TextChoices):
    EXPIRY = "expiry", _("expiry of contract period")
    VOLUNTARY = "voluntary", _("voluntary resignation")
    COMPANY = "company", _("dismissal or company reasons")
    JOB_CHANGE = "job_change", _("change of job")
    OTHER = "other", _("other")


class EmploymentContract(TrackedModel):
    """雇用契約の見出し。契約の相手は受入れ機関（派遣は不可なので、就業場所はその事業所に限る）。

    current_starts_on / current_ends_on は最新の版の期間を写したもの（版の追加と同じ
    トランザクションで更新する）。同じ外国人の契約期間の重なりを DB の排他制約で拒否するために使う。
    状態（予定／有効／終了）は列で持たず、services.contracts.status_of で決める。
    """

    worker = models.ForeignKey(
        ForeignWorker,
        verbose_name=_("foreign worker"),
        on_delete=models.PROTECT,
        related_name="employment_contracts",
    )
    host_organization = models.ForeignKey(
        HostOrganization,
        verbose_name=_("host organization"),
        on_delete=models.PROTECT,
        related_name="employment_contracts",
    )
    current_starts_on = models.DateField(_("starts on"), editable=False)
    current_ends_on = models.DateField(_("ends on"), null=True, blank=True, editable=False)
    terminated_on = models.DateField(_("terminated on"), null=True, blank=True)
    termination_reason = models.CharField(
        _("termination reason"), max_length=16, choices=TerminationReason.choices, blank=True
    )
    termination_note = models.TextField(_("termination note"), blank=True)

    class Meta:
        verbose_name = _("employment contract")
        verbose_name_plural = _("employment contracts")
        ordering = ["-current_starts_on", "-pk"]
        constraints = [
            models.CheckConstraint(
                condition=Q(terminated_on__isnull=True)
                | Q(terminated_on__gte=F("current_starts_on")),
                name="contracts_terminated_after_start",
                violation_error_message=_(
                    "The termination date must be on or after the start date."
                ),
            ),
            models.CheckConstraint(
                condition=Q(terminated_on__isnull=True) | ~Q(termination_reason=""),
                name="contracts_termination_has_reason",
                violation_error_message=_("Select the termination reason."),
            ),
            # 同じ外国人の契約期間を重ねない
            # （開始日〜版の終了日と終了の登録日の早いほう。両端を含む）
            ExclusionConstraint(
                name="contracts_employmentcontract_no_overlap",
                expressions=[
                    (
                        DateRange(
                            "current_starts_on",
                            Least("current_ends_on", "terminated_on"),
                            RangeBoundary(inclusive_lower=True, inclusive_upper=True),
                        ),
                        RangeOperators.OVERLAPS,
                    ),
                    ("worker", RangeOperators.EQUAL),
                ],
                violation_error_message=_(
                    "This period overlaps another employment contract of the same foreign worker."
                ),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.worker} / {self.host_organization} ({self.current_starts_on}~)"


class WageType(models.TextChoices):
    MONTHLY = "monthly", _("monthly salary")
    DAILY = "daily", _("daily wage")
    HOURLY = "hourly", _("hourly wage")


class Renewal(models.TextChoices):
    AUTOMATIC = "automatic", _("renewed automatically")
    POSSIBLE = "possible", _("may be renewed")
    NONE = "none", _("not renewed")


class RenewalCriterion(models.TextChoices):
    WORKLOAD = "workload", _("workload at the end of the contract period")
    PERFORMANCE = "performance", _("work performance and attitude")
    ABILITY = "ability", _("ability to perform the work")
    BUSINESS = "business", _("business condition of the company")
    PROGRESS = "progress", _("progress of the work")
    OTHER = "other", _("other")


class ConversionChange(models.TextChoices):
    """無期転換後の労働条件の変更の有無（雇用条件書 Ⅰ。「有」は別紙2）。"""

    NONE = "none", _("no change")
    CHANGED = "changed", _("changed (annex 2)")


class VariableHoursUnit(models.TextChoices):
    ONE_WEEK = "one_week", _("one-week unit")
    ONE_MONTH = "one_month", _("one-month unit")
    ONE_YEAR = "one_year", _("one-year unit")


class Weekday(models.TextChoices):
    MONDAY = "mon", _("Monday")
    TUESDAY = "tue", _("Tuesday")
    WEDNESDAY = "wed", _("Wednesday")
    THURSDAY = "thu", _("Thursday")
    FRIDAY = "fri", _("Friday")
    SATURDAY = "sat", _("Saturday")
    SUNDAY = "sun", _("Sunday")


class PaymentMethod(models.TextChoices):
    BANK_TRANSFER = "bank_transfer", _("bank transfer")
    CASH = "cash", _("cash")


class Insurance(models.TextChoices):
    EMPLOYEES_PENSION = "employees_pension", _("employees' pension insurance")
    HEALTH = "health", _("health insurance")
    EMPLOYMENT = "employment", _("employment insurance")
    WORKERS_COMPENSATION = "workers_compensation", _("workers' accident compensation insurance")
    NATIONAL_PENSION = "national_pension", _("national pension")
    NATIONAL_HEALTH = "national_health", _("national health insurance")
    OTHER = "other", _("other")


def _articles(label: Any) -> models.CharField[Any, Any]:
    """雇用条件書の「詳細は、就業規則 第 条～第 条」（Ⅳ・Ⅴ・Ⅵ・Ⅷ）。"""
    return models.CharField(
        label, max_length=100, blank=True, help_text=_("e.g. Articles 15 to 18, Article 20")
    )


def _rate(label: Any) -> models.DecimalField[Any, Any]:
    return models.DecimalField(label, max_digits=4, decimal_places=1, null=True, blank=True)


class EmploymentContractVersion(TrackedModel):
    """雇用契約の版。作成後は変更しない（save で既存行の更新を拒否する）。

    項目は雇用条件書（参考様式第1-6号）の章立て Ⅰ〜Ⅸ と別紙1 に合わせて並べている。
    """

    contract = models.ForeignKey(
        EmploymentContract,
        verbose_name=_("employment contract"),
        on_delete=models.PROTECT,
        related_name="versions",
    )
    version = models.PositiveIntegerField(_("version"), editable=False)
    change_reason = models.TextField(_("change reason"), blank=True)
    changed_on = models.DateField(
        _("changed on"),
        help_text=_("The date the change takes effect (the start date for version 1)."),
    )
    signed_on = models.DateField(
        _("signed on"), null=True, blank=True, help_text=_("Date of conclusion or agreement.")
    )

    # Ⅰ．雇用契約期間
    starts_on = models.DateField(_("starts on"))
    ends_on = models.DateField(_("ends on"), null=True, blank=True)
    expected_entry_on = models.DateField(_("expected entry date"), null=True, blank=True)
    renewal = models.CharField(_("renewal"), max_length=16, choices=Renewal.choices)
    renewal_criteria = ChoiceArrayField(
        models.CharField(max_length=16, choices=RenewalCriterion.choices),
        verbose_name=_("renewal criteria"),
        default=list,
        blank=True,
    )
    renewal_criteria_other = models.CharField(
        _("other renewal criteria"), max_length=200, blank=True
    )
    has_renewal_limit = models.BooleanField(_("has renewal limit"), default=False)
    renewal_limit_count = models.PositiveSmallIntegerField(
        _("renewal limit (times)"), null=True, blank=True
    )
    renewal_limit_years = models.PositiveSmallIntegerField(
        _("renewal limit (total years)"), null=True, blank=True
    )
    # 無期転換（労働契約法第18条。労働基準法施行規則第5条第5項・第6項。ADR-0016）
    indefinite_conversion = models.BooleanField(
        _("can apply for an indefinite contract during this period"),
        default=False,
        help_text=_(
            "Check when the total period of fixed-term contracts with the same employer "
            "exceeds 5 years during this contract. The conversion date is the day after the end "
            "date."
        ),
    )
    conversion_conditions_change = models.CharField(
        _("change of working conditions after conversion"),
        max_length=16,
        choices=ConversionChange.choices,
        blank=True,
    )

    # Ⅱ．就業の場所（直接雇用のみ。派遣は不可）
    site = models.ForeignKey(
        HostSite, verbose_name=_("host site"), on_delete=models.PROTECT, related_name="+"
    )
    workplace_change_scope = models.TextField(
        _("possible change of workplace"),
        blank=True,
        help_text=_("Leave blank if there is no possibility of change."),
    )

    # Ⅲ．従事すべき業務の内容（分野は事業所の分野）
    categories = models.ManyToManyField(
        OperationCategory, verbose_name=_("operation categories"), related_name="+"
    )
    duties = models.TextField(_("duties"))
    duty_change_scope = models.TextField(
        _("possible change of duties"),
        blank=True,
        help_text=_("Leave blank if there is no possibility of change."),
    )

    # Ⅳ．労働時間等
    start_time = models.TimeField(_("start time"))
    end_time = models.TimeField(_("end time"))
    daily_working_minutes = models.PositiveSmallIntegerField(_("daily working hours (minutes)"))
    variable_hours_unit = models.CharField(
        _("variable working hours system"),
        max_length=16,
        choices=VariableHoursUnit.choices,
        blank=True,
        help_text=_(
            "Leave blank if not applied. One-year unit requires the calendar and agreement."
        ),
    )
    break_minutes = models.PositiveSmallIntegerField(_("break (minutes)"))
    weekly_working_minutes = models.PositiveIntegerField(_("weekly working hours (minutes)"))
    monthly_working_minutes = models.PositiveIntegerField(
        _("monthly working hours (minutes)"), null=True, blank=True
    )
    annual_working_minutes = models.PositiveIntegerField(
        _("annual working hours (minutes)"), null=True, blank=True
    )
    weekly_working_days = models.DecimalField(
        _("weekly working days"), max_digits=3, decimal_places=1
    )
    monthly_working_days = models.DecimalField(
        _("monthly working days"), max_digits=4, decimal_places=1, null=True, blank=True
    )
    annual_working_days = models.PositiveSmallIntegerField(
        _("annual working days"), null=True, blank=True
    )
    has_overtime = models.BooleanField(_("overtime work"), default=True)
    work_rules_articles_hours = _articles(_("work rules articles (working hours)"))

    # Ⅴ．休日
    regular_holiday_weekdays = ChoiceArrayField(
        models.CharField(max_length=3, choices=Weekday.choices),
        verbose_name=_("regular holidays (weekdays)"),
        default=list,
        blank=True,
    )
    national_holidays_off = models.BooleanField(_("national holidays are days off"), default=True)
    regular_holidays_other = models.CharField(
        _("other regular holidays"), max_length=200, blank=True
    )
    annual_holidays = models.PositiveSmallIntegerField(_("annual holidays"))
    irregular_holidays = models.CharField(_("irregular holidays"), max_length=200, blank=True)
    work_rules_articles_holidays = _articles(_("work rules articles (holidays)"))

    # Ⅵ．休暇
    paid_leave_days = models.PositiveSmallIntegerField(_("annual paid leave after 6 months (days)"))
    has_early_paid_leave = models.BooleanField(_("paid leave before 6 months"), default=False)
    early_paid_leave_months = models.PositiveSmallIntegerField(
        _("paid leave before 6 months: after (months)"), null=True, blank=True
    )
    early_paid_leave_days = models.PositiveSmallIntegerField(
        _("paid leave before 6 months: days"), null=True, blank=True
    )
    other_paid_leave = models.CharField(_("other paid leave"), max_length=200, blank=True)
    other_unpaid_leave = models.CharField(_("other unpaid leave"), max_length=200, blank=True)
    temporary_return_leave_note = models.TextField(
        _("temporary return home leave (notes)"),
        blank=True,
        help_text=_(
            "The form states that leave is granted within paid and other leave. "
            "Write only additional arrangements."
        ),
    )
    work_rules_articles_leave = _articles(_("work rules articles (leave)"))

    # Ⅶ．賃金（別紙1「賃金の支払」を含む）
    wage_type = models.CharField(_("wage type"), max_length=16, choices=WageType.choices)
    base_wage = models.PositiveIntegerField(_("base wage (yen)"))
    hourly_equivalent = models.PositiveIntegerField(
        _("hourly equivalent (yen)"),
        null=True,
        blank=True,
        help_text=_("For monthly or daily wage."),
    )
    monthly_equivalent = models.PositiveIntegerField(
        _("monthly equivalent (yen)"),
        null=True,
        blank=True,
        help_text=_("For daily or hourly wage."),
    )
    overtime_rate_within_60h = _rate(_("overtime premium, statutory, up to 60 h/month (%)"))
    overtime_rate_over_60h = _rate(_("overtime premium, statutory, over 60 h/month (%)"))
    overtime_rate_scheduled = _rate(_("overtime premium beyond scheduled hours (%)"))
    holiday_rate_statutory = _rate(_("holiday premium, statutory holiday (%)"))
    holiday_rate_non_statutory = _rate(_("holiday premium, non-statutory holiday (%)"))
    night_rate = _rate(_("late-night premium (%)"))
    wage_closing_day = models.CharField(
        _("wage closing day"), max_length=30, help_text=_("e.g. end of month, 15th")
    )
    wage_payment_day = models.CharField(
        _("wage payment day"), max_length=30, help_text=_("e.g. 25th of the following month")
    )
    payment_method = models.CharField(
        _("payment method"), max_length=16, choices=PaymentMethod.choices
    )
    has_deduction_agreement = models.BooleanField(
        _("deductions under labor-management agreement"), default=False
    )
    has_raise = models.BooleanField(_("pay raise"), default=False)
    raise_note = models.CharField(_("pay raise (timing, amount)"), max_length=200, blank=True)
    has_bonus = models.BooleanField(_("bonus"), default=False)
    bonus_note = models.CharField(_("bonus (timing, amount)"), max_length=200, blank=True)
    has_retirement_allowance = models.BooleanField(_("retirement allowance"), default=False)
    retirement_allowance_note = models.CharField(
        _("retirement allowance (timing, amount)"), max_length=200, blank=True
    )
    leave_allowance_rate = _rate(_("leave allowance rate (%)"))

    # Ⅷ．退職に関する事項
    resignation_notice_days = models.PositiveSmallIntegerField(
        _("notice period for voluntary resignation (days)"), null=True, blank=True
    )
    work_rules_articles_resignation = _articles(_("work rules articles (resignation)"))

    # Ⅸ．その他
    insurances = ChoiceArrayField(
        models.CharField(max_length=24, choices=Insurance.choices),
        verbose_name=_("social and labor insurance"),
        default=list,
        blank=True,
    )
    insurance_other = models.CharField(_("other insurance"), max_length=100, blank=True)
    hiring_health_check_on = models.DateField(
        _("health check at hiring (month)"), null=True, blank=True
    )
    first_periodic_health_check_on = models.DateField(
        _("first periodic health check (month)"), null=True, blank=True
    )
    periodic_health_check_interval = models.CharField(
        _("periodic health check interval"),
        max_length=50,
        blank=True,
        help_text=_("e.g. every year"),
    )
    # 窓口（Ⅸ４・５）。T34 までは 1 つの文で持っていた（下の 2 つ。古い版の値として残す）。
    # T34 からは様式の欄（部署名・担当者職氏名・連絡先）ごとに持つ（T34-D4、ADR-0023）。
    treatment_explanation_contact = models.TextField(
        _("contact for explanation of treatment differences (until T34)"),
        blank=True,
        help_text=_("Department, title and name of the person in charge, contact."),
    )
    consultation_contact = models.TextField(
        _("contact for employment management consultation (until T34)"),
        blank=True,
        help_text=_("Department, title and name of the person in charge, contact."),
    )
    treatment_contact_department = models.CharField(
        _("treatment differences contact: department"), max_length=100, blank=True
    )
    treatment_contact_person = models.CharField(
        _("treatment differences contact: title and name of the person in charge"),
        max_length=100,
        blank=True,
    )
    treatment_contact_phone = models.CharField(
        _("treatment differences contact: contact details"), max_length=50, blank=True
    )
    consultation_contact_department = models.CharField(
        _("consultation contact: department"), max_length=100, blank=True
    )
    consultation_contact_person = models.CharField(
        _("consultation contact: title and name of the person in charge"),
        max_length=100,
        blank=True,
    )
    consultation_contact_phone = models.CharField(
        _("consultation contact: contact details"), max_length=50, blank=True
    )
    work_rules_location = models.CharField(
        _("where the work rules can be checked"), max_length=200, blank=True
    )

    # 日本人と同等以上の報酬の根拠（F11 の生成前バリデーションで使う）
    comparable_japanese_note = models.TextField(
        _("comparable Japanese worker (position, wage rules)"), blank=True
    )
    comparable_japanese_wage = models.PositiveIntegerField(
        _("comparable Japanese worker's wage (yen)"), null=True, blank=True
    )
    attachments = GenericRelation(Attachment)

    class Meta:
        verbose_name = _("employment contract version")
        verbose_name_plural = _("employment contract versions")
        ordering = ["contract", "-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["contract", "version"], name="contracts_version_unique"
            ),
            models.CheckConstraint(
                condition=Q(ends_on__isnull=True) | Q(ends_on__gte=F("starts_on")),
                name="contracts_version_period",
                violation_error_message=_("The end date must be on or after the start date."),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.contract} v{self.version}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise ImmutableRowError("employment contract versions cannot be changed")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ImmutableRowError("employment contract versions cannot be deleted")


class VersionDetail(TrackedModel):
    """版の明細行（交代制の勤務時間・手当・控除）。版と同じく作成後は変更しない。"""

    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise ImmutableRowError("details of a contract version cannot be changed")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ImmutableRowError("details of a contract version cannot be deleted")


class EmploymentContractShift(VersionDetail):
    """交代制の勤務時間の組合せ（雇用条件書 Ⅳ-1(2)）。"""

    version = models.ForeignKey(
        EmploymentContractVersion,
        verbose_name=_("employment contract version"),
        on_delete=models.PROTECT,
        related_name="shifts",
    )
    start_time = models.TimeField(_("start time"))
    end_time = models.TimeField(_("end time"))
    applies_to = models.CharField(_("applies to"), max_length=100, blank=True)
    daily_working_minutes = models.PositiveSmallIntegerField(_("daily working hours (minutes)"))

    class Meta:
        verbose_name = _("shift")
        verbose_name_plural = _("shifts")
        ordering = ["version", "start_time", "pk"]

    def __str__(self) -> str:
        return f"{self.start_time:%H:%M}-{self.end_time:%H:%M}"


class EmploymentContractAllowance(VersionDetail):
    """諸手当（別紙1 の 2。時間外労働の割増賃金は除く）。固定残業代は時間数を入れる。"""

    version = models.ForeignKey(
        EmploymentContractVersion,
        verbose_name=_("employment contract version"),
        on_delete=models.PROTECT,
        related_name="allowances",
    )
    name = models.CharField(_("name"), max_length=50)
    amount = models.PositiveIntegerField(_("amount per month (yen)"))
    calculation_method = models.CharField(_("calculation method"), max_length=200, blank=True)
    fixed_overtime_hours = models.DecimalField(
        _("fixed overtime pay: hours covered"),
        max_digits=5,
        decimal_places=1,
        null=True,
        blank=True,
        help_text=_("Only for fixed overtime pay."),
    )

    class Meta:
        verbose_name = _("allowance")
        verbose_name_plural = _("allowances")
        ordering = ["version", "pk"]

    def __str__(self) -> str:
        return f"{self.name}: {self.amount}"


class DeductionKind(models.TextChoices):
    TAX = "tax", _("tax")
    SOCIAL_INSURANCE = "social_insurance", _("social insurance premiums")
    EMPLOYMENT_INSURANCE = "employment_insurance", _("employment insurance premiums")
    FOOD = "food", _("food")
    HOUSING = "housing", _("housing")
    UTILITIES = "utilities", _("utilities")
    OTHER = "other", _("other")


class EmploymentContractDeduction(VersionDetail):
    """賃金支払時に控除する項目（別紙1 の 4）。"""

    version = models.ForeignKey(
        EmploymentContractVersion,
        verbose_name=_("employment contract version"),
        on_delete=models.PROTECT,
        related_name="deductions",
    )
    kind = models.CharField(_("kind"), max_length=24, choices=DeductionKind.choices)
    name = models.CharField(_("name"), max_length=50, blank=True, help_text=_("For other."))
    amount = models.PositiveIntegerField(_("approximate amount per month (yen)"))

    class Meta:
        verbose_name = _("deduction")
        verbose_name_plural = _("deductions")
        ordering = ["version", "pk"]

    def __str__(self) -> str:
        return f"{self.name or self.get_kind_display()}: {self.amount}"


class WageComparison(models.TextChoices):
    """報酬に関する説明書（参考様式第1-4号）の 2 と 3 のどちらで説明するか。"""

    COMPARABLE = "comparable", _("there is a comparable Japanese worker (section 2)")
    CLOSEST = (
        "closest",
        _("no comparable Japanese worker; compare with the closest duties (section 3)"),
    )
    NONE = "none", _("there is no Japanese worker to compare with")


class WageUnit(models.TextChoices):
    """第1-4号の報酬は、月給か時間給のどちらかにそろえて書く（注意３）。"""

    MONTHLY = "monthly", _("monthly salary")
    HOURLY = "hourly", _("hourly wage")


class ContractWageExplanation(TrackedModel):
    """報酬の説明（参考様式第1-4号「特定技能外国人の報酬に関する説明書」の元データ。T34-D2、ADR-0023）。

    雇用契約の版と 1 対 1 の別の表。版と違って編集できる（更新は監査ログに残す）。生成のときは
    スナップショットに残る。新しい版を作るときは前の版の説明を写す。契約の版の差分・届出の判定・
    「差分の無い版は保存しない」には関わらない。
    申請人の氏名・年齢・性別は外国人の台帳から、報酬は版の基本賃金（換算額）から作る。
    """

    version = models.OneToOneField(
        EmploymentContractVersion,
        verbose_name=_("employment contract version"),
        on_delete=models.PROTECT,
        related_name="wage_explanation",
    )
    comparison = models.CharField(
        _("comparison"), max_length=16, choices=WageComparison.choices, blank=True
    )
    wage_unit = models.CharField(
        _("unit of the wages on the form"),
        max_length=16,
        choices=WageUnit.choices,
        blank=True,
        help_text=_("Write all wages on the form in the same unit (note 3)."),
    )
    # 1　申請人に対する報酬
    applicant_duties = models.TextField(
        _("applicant: position, duties and degree of responsibility"), blank=True
    )
    applicant_experience_years = models.DecimalField(
        _("applicant: years of experience in the duties"),
        max_digits=4,
        decimal_places=1,
        null=True,
        blank=True,
    )
    applicant_other = models.TextField(
        _("applicant: other (allowances and other special notes)"), blank=True
    )
    # 2　比較対象となる日本人労働者がいる場合 ／ 3　いない場合（最も近い職務を担う日本人労働者）
    japanese_duties = models.TextField(
        _("Japanese worker: position, duties and degree of responsibility"),
        blank=True,
        help_text=_(
            "For the worker with the closest duties, also write in detail how they differ "
            "from the applicant."
        ),
    )
    japanese_age = models.PositiveSmallIntegerField(
        _("Japanese worker: age"), null=True, blank=True
    )
    japanese_sex = models.CharField(
        _("Japanese worker: sex"), max_length=8, choices=Sex.choices, blank=True
    )
    japanese_experience_years = models.DecimalField(
        _("Japanese worker: years of experience"),
        max_digits=4,
        decimal_places=1,
        null=True,
        blank=True,
    )
    japanese_wage = models.PositiveIntegerField(
        _("Japanese worker: wage (yen, in the unit above)"), null=True, blank=True
    )
    has_wage_rules = models.BooleanField(_("there are wage rules"), null=True, blank=True)
    rules_wage = models.PositiveIntegerField(
        _("wage under the wage rules (yen, in the unit above)"),
        null=True,
        blank=True,
        help_text=_(
            "The wage to be paid under the wage rules to a Japanese worker with the same "
            "position, duties and responsibility. Attach the wage rules."
        ),
    )
    reason = models.TextField(
        _("reason the wage is equal to or higher than that of a Japanese worker"), blank=True
    )
    other = models.TextField(_("other (special notes)"), blank=True)

    class Meta:
        verbose_name = _("wage explanation")
        verbose_name_plural = _("wage explanations")

    def __str__(self) -> str:
        return f"{self.version}"
