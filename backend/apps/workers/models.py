"""外国人台帳（仕様書第7章 foreign_workers / residence_records / passport_records /
worker_addresses / contacts）。

在留情報・旅券・住所・連絡先は上書きしない履歴行（制約7）。入力ミスは取り消しの印を付けて
正しい行を足す。最新行は DB ビュー（Latest* モデル）で取る。
"""

import datetime

from django.conf import settings
from django.contrib.contenttypes.fields import GenericRelation
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import RangeOperators
from django.db import models
from django.db.models import Q
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from apps.core.fields import BlindIndexField, EncryptedCharField
from apps.core.models import (
    Attachment,
    Country,
    Staff,
    StaffPeriod,
    SupportLanguage,
    TrackedModel,
)
from services import passport, residence_card


class ConfirmedValue(models.Model):
    """確定値の最終確認者・確認日時（第7章。将来の本人申告→担当者確認に備える）。"""

    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("confirmed by"),
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
        editable=False,
    )
    confirmed_at = models.DateTimeField(_("confirmed at"), null=True, blank=True, editable=False)

    class Meta:
        abstract = True


class HistoryRecord(TrackedModel):
    """履歴行の共通列。作成後は変更せず、誤りは取り消して新しい行を足す。"""

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

    class Meta:
        abstract = True


class Sex(models.TextChoices):
    MALE = "male", _("male")
    FEMALE = "female", _("female")


class WorkerStatus(models.TextChoices):
    PRE_ENTRY = "pre_entry", _("before entry")
    RESIDING = "residing", _("residing")
    LEFT_JOB = "left_job", _("left job")
    RETURNED = "returned", _("returned home")


class ForeignWorker(TrackedModel, ConfirmedValue):
    """支援対象の外国人（特定技能1号）。"""

    name_romaji = models.CharField(_("name (romaji)"), max_length=100)
    name_kanji = models.CharField(_("name (kanji)"), max_length=100, blank=True)
    name_native = models.CharField(_("name (native script)"), max_length=100, blank=True)
    # カナ（任意。仕様書の氏名3表記の外。検索と画面の読みのため。修正 1 回目 A4、ADR-0028）
    name_kana = models.CharField(
        _("name (katakana)"),
        max_length=100,
        blank=True,
        help_text=_("Optional. Katakana; hiragana is converted to katakana."),
    )
    nationality = models.ForeignKey(
        Country, verbose_name=_("nationality or region"), on_delete=models.PROTECT, related_name="+"
    )
    birth_date = models.DateField(_("date of birth"))
    sex = models.CharField(_("sex"), max_length=8, choices=Sex.choices)
    support_language = models.ForeignKey(
        SupportLanguage,
        verbose_name=_("support language"),
        on_delete=models.PROTECT,
        related_name="foreign_workers",
    )
    assigned_staff = models.ForeignKey(
        Staff,
        verbose_name=_("assigned staff"),
        on_delete=models.PROTECT,
        related_name="foreign_workers",
        null=True,
        blank=True,
    )
    status = models.CharField(
        _("status"), max_length=16, choices=WorkerStatus.choices, default=WorkerStatus.PRE_ENTRY
    )
    # 一括申請の欄から逆算して足した項目（T46a。ADR-0038）。どれも任意（申請の前の検査が知らせる）
    birthplace = models.CharField(_("place of birth"), max_length=40, blank=True)
    has_relatives_in_japan = models.BooleanField(
        _("relatives or housemates in Japan"), null=True, blank=True
    )
    has_work_history = models.BooleanField(_("work history"), null=True, blank=True)
    ssw_months_outside_ledger = models.PositiveSmallIntegerField(
        _("months as Specified Skilled Worker (i) not in this ledger"),
        null=True,
        blank=True,
        help_text=_(
            "Months of stay as Specified Skilled Worker (i) before registration in this system "
            "(for example, with a previous employer). Used for the total period of stay."
        ),
    )
    # 無効化（誤登録・重複などで台帳の対象から外す。状態とは別の軸。T13e）
    deactivated_at = models.DateTimeField(
        _("deactivated at"), null=True, blank=True, editable=False
    )
    deactivated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("deactivated by"),
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
        editable=False,
    )
    deactivation_reason = models.CharField(
        _("deactivation reason"), max_length=200, blank=True, editable=False
    )
    # 将来の本人ポータル用（MVP では空のまま）
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        verbose_name=_("login user"),
        on_delete=models.PROTECT,
        related_name="foreign_worker",
        null=True,
        blank=True,
    )
    # 健康診断の書類（要配慮の添付。T23）
    attachments = GenericRelation(Attachment)

    class Meta:
        verbose_name = _("foreign worker")
        verbose_name_plural = _("foreign workers")
        ordering = ["name_romaji", "pk"]

    def __str__(self) -> str:
        return self.name_kanji or self.name_romaji


class StatusOfResidence(models.TextChoices):
    SSW_1 = "ssw_1", _("Specified Skilled Worker (i)")
    SSW_2 = "ssw_2", _("Specified Skilled Worker (ii)")
    DESIGNATED_TRANSITION = "designated_transition", _("Designated Activities (transition)")
    TECHNICAL_INTERN_2 = "technical_intern_2", _("Technical Intern Training (ii)")
    TECHNICAL_INTERN_3 = "technical_intern_3", _("Technical Intern Training (iii)")
    STUDENT = "student", _("Student")
    OTHER = "other", _("Other")


class TechnicalInternType(models.TextChoices):
    """技能実習の区分（一括申請ツールの在留資格がイ・ロで分かれている。T46-D8）。"""

    I = "i", _("(i) individual enterprise type")  # noqa: E741
    RO = "ro", _("(ro) supervising organization type")


class ApplicationType(models.TextChoices):
    CERTIFICATE_OF_ELIGIBILITY = "coe", _("Certificate of Eligibility")
    CHANGE = "change", _("Change of status of residence")
    EXTENSION = "extension", _("Extension of period of stay")
    # 以下 2 つは番号が変わり、在留期限は変わらない
    CARD_REISSUE = "card_reissue", _("Residence card reissue (lost, damaged, etc.)")
    PARTICULARS_CHANGE = "particulars_change", _("Change of particulars (new card issued)")


class ResidenceRecord(HistoryRecord):
    """在留情報の履歴。最新行は LatestResidenceRecord（許可日・交付日が新しい順、同日は ID 順）。"""

    worker = models.ForeignKey(
        ForeignWorker,
        verbose_name=_("foreign worker"),
        on_delete=models.PROTECT,
        related_name="residence_records",
    )
    card_number = EncryptedCharField(
        _("residence card number"),
        max_length=12,
        blank=True,
        validators=[residence_card.validate],
        help_text=_("Leave blank before entry (no residence card yet)."),
    )
    card_number_index = BlindIndexField(source="card_number")
    status_of_residence = models.CharField(
        _("status of residence"), max_length=32, choices=StatusOfResidence.choices
    )
    expires_on = models.DateField(_("period of stay expires on"))
    permitted_on = models.DateField(
        _("permitted on"),
        help_text=_("For card reissue and change of particulars, enter the issue date."),
    )
    application_type = models.CharField(
        _("application type"), max_length=32, choices=ApplicationType.choices
    )
    # 在留期間（長さ。一括申請ツールのコード CB20）と技能実習のイ・ロ（T46a。新しい行から）
    period_of_stay = models.CharField(_("period of stay"), max_length=7, blank=True)
    technical_intern_type = models.CharField(
        _("type of technical intern training"),
        max_length=2,
        choices=TechnicalInternType.choices,
        blank=True,
        help_text=_("Only for Technical Intern Training (ii) and (iii)."),
    )

    class Meta:
        verbose_name = _("residence record")
        verbose_name_plural = _("residence records")
        ordering = ["-permitted_on", "-pk"]
        constraints = [
            # 同じ番号が別の人に紐づかない（同じ人の複数行・取り消し済みの行・空の番号は許す）
            ExclusionConstraint(
                name="workers_residence_card_one_worker",
                expressions=[
                    ("card_number_index", RangeOperators.EQUAL),
                    ("worker", RangeOperators.NOT_EQUAL),
                ],
                condition=Q(voided_at__isnull=True),
                violation_error_message=_(
                    "This residence card number is registered to another foreign worker."
                ),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.worker}: {self.get_status_of_residence_display()} ~{self.expires_on}"


class PassportRecord(HistoryRecord):
    """旅券の履歴。最新行は LatestPassportRecord（発行日が新しい順）。"""

    worker = models.ForeignKey(
        ForeignWorker,
        verbose_name=_("foreign worker"),
        on_delete=models.PROTECT,
        related_name="passport_records",
    )
    number = EncryptedCharField(_("passport number"), max_length=12, validators=[passport.validate])
    number_index = BlindIndexField(source="number")
    issuing_country = models.ForeignKey(
        Country,
        verbose_name=_("issuing country or region"),
        on_delete=models.PROTECT,
        related_name="+",
    )
    issued_on = models.DateField(_("issued on"))
    expires_on = models.DateField(_("expires on"))

    class Meta:
        verbose_name = _("passport record")
        verbose_name_plural = _("passport records")
        ordering = ["-issued_on", "-pk"]
        constraints = [
            ExclusionConstraint(
                name="workers_passport_one_worker",
                expressions=[
                    ("number_index", RangeOperators.EQUAL),
                    ("worker", RangeOperators.NOT_EQUAL),
                ],
                condition=Q(voided_at__isnull=True),
                violation_error_message=_(
                    "This passport number is registered to another foreign worker."
                ),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.worker}: {self.issuing_country} ~{self.expires_on}"


class StaffAssignment(StaffPeriod):
    """外国人の担当者の履歴（T45。ADR-0037）。今の担当者は ForeignWorker.assigned_staff に写す。"""

    worker = models.ForeignKey(
        ForeignWorker,
        verbose_name=_("foreign worker"),
        on_delete=models.CASCADE,
        related_name="staff_assignments",
    )

    class Meta:
        verbose_name = _("assigned staff (history)")
        verbose_name_plural = _("assigned staff (history)")
        ordering = ["-starts_on", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["worker", "starts_on"],
                condition=Q(voided_at__isnull=True),
                name="workers_staff_assignment_one_per_day",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.worker}: {self.starts_on}~"


class WorkerAddress(HistoryRecord, ConfirmedValue):
    """日本の住所の履歴。最新行は LatestWorkerAddress（適用開始日が新しい順）。"""

    worker = models.ForeignKey(
        ForeignWorker,
        verbose_name=_("foreign worker"),
        on_delete=models.PROTECT,
        related_name="addresses",
    )
    postal_code = models.CharField(_("postal code"), max_length=8)
    address = models.CharField(_("residential address"), max_length=255)
    effective_from = models.DateField(_("effective from"))
    # 住所を分けた値（一括申請ツールの都道府県 CA69_1・市区町村 CA69 のコードと以降。保存のときに
    # 住所から最長一致で分ける。分けられなければ空。T46-D4）
    prefecture_code = models.CharField(
        _("prefecture code"), max_length=6, blank=True, editable=False
    )
    municipality_code = models.CharField(
        _("municipality code"), max_length=6, blank=True, editable=False
    )
    address_rest = models.CharField(
        _("rest of the address"), max_length=255, blank=True, editable=False
    )

    class Meta:
        verbose_name = _("worker address")
        verbose_name_plural = _("worker addresses")
        ordering = ["-effective_from", "-pk"]

    def __str__(self) -> str:
        return f"{self.worker}: {self.effective_from}~"


class WorkerContact(HistoryRecord, ConfirmedValue):
    """連絡先（電話・緊急連絡先・母国の連絡先）の履歴。最新行は LatestWorkerContact。"""

    worker = models.ForeignKey(
        ForeignWorker,
        verbose_name=_("foreign worker"),
        on_delete=models.PROTECT,
        related_name="contacts",
    )
    phone = models.CharField(_("phone number"), max_length=20, blank=True)
    email = models.EmailField(_("email"), blank=True)
    emergency_name = models.CharField(_("emergency contact name"), max_length=100, blank=True)
    emergency_relationship = models.CharField(
        _("emergency contact relationship"), max_length=50, blank=True
    )
    emergency_phone = models.CharField(_("emergency contact phone"), max_length=30, blank=True)
    home_country_address = models.CharField(_("home country address"), max_length=255, blank=True)
    home_country_phone = models.CharField(_("home country phone"), max_length=30, blank=True)
    # 空でもよい（本国の住所・連絡先など、いつからか分からないもの。修正 1 回目 B6）。空の行は
    # 登録した日を適用開始日とみなして並べる（最新行のビューも同じ）
    effective_from = models.DateField(
        _("effective from"),
        null=True,
        blank=True,
        help_text=_("Leave blank if unknown; the date of registration is used for ordering."),
    )

    class Meta:
        verbose_name = _("contact")
        verbose_name_plural = _("contacts")
        ordering = [Coalesce("effective_from", TruncDate("created_at")).desc(), "-pk"]

    def __str__(self) -> str:
        return f"{self.worker}: {self.effective_on}~"

    @property
    def effective_on(self) -> datetime.date:
        """並び順の日付（適用開始日。空なら登録した日）。"""
        return self.effective_from or timezone.localdate(self.created_at)


# --- 一括申請の欄から逆算して足した履歴行（T46a。ADR-0038）---------------------------------
# 選択肢の値は一括申請ツールのコード（services/data/immigration_codes.json。T46-D3）。


class WorkerRelative(HistoryRecord):
    """在日親族・同居者（T46-D2）。在留カード番号（特別永住者証明書番号）は列暗号化する。"""

    worker = models.ForeignKey(
        ForeignWorker,
        verbose_name=_("foreign worker"),
        on_delete=models.PROTECT,
        related_name="relatives",
    )
    relationship = models.CharField(_("relationship"), max_length=4)  # CB06_2
    name = models.CharField(_("full name"), max_length=26)
    birth_date = models.DateField(_("date of birth"), null=True, blank=True)
    nationality_code = models.CharField(_("nationality or region"), max_length=3)  # CB79
    lives_together = models.BooleanField(_("lives (or will live) together"), default=False)
    workplace = models.CharField(_("place of work or school"), max_length=60, blank=True)
    card_number = EncryptedCharField(
        _("residence card or special permanent resident certificate number"),
        max_length=12,
        blank=True,
    )

    class Meta:
        verbose_name = _("relative or housemate in Japan")
        verbose_name_plural = _("relatives and housemates in Japan")
        ordering = ["pk"]

    def __str__(self) -> str:
        return f"{self.worker}: {self.name}"


class QualificationArea(models.TextChoices):
    SKILL = "skill", _("skill level")
    JAPANESE = "japanese", _("Japanese language ability")


class WorkerQualification(HistoryRecord):
    """技能水準・日本語能力の評価（試験の合格など。T46-D7）。合格証明書は添付で持つ。"""

    worker = models.ForeignKey(
        ForeignWorker,
        verbose_name=_("foreign worker"),
        on_delete=models.PROTECT,
        related_name="qualifications",
    )
    area = models.CharField(_("area"), max_length=16, choices=QualificationArea.choices)
    evaluation = models.CharField(_("evaluation method"), max_length=2)  # CC06
    exam = models.CharField(_("test passed"), max_length=3, blank=True)  # CC09・CC10
    exam_country = models.CharField(
        _("country or region where the test was taken"), max_length=3, blank=True
    )  # CB79
    passed_on = models.DateField(_("passed on"), null=True, blank=True)
    level = models.CharField(pgettext_lazy("test", "level"), max_length=30, blank=True)
    other_proof = models.CharField(
        _("proof by another evaluation method"), max_length=60, blank=True
    )
    attachments = GenericRelation(Attachment)

    class Meta:
        verbose_name = _("skill or Japanese language qualification")
        verbose_name_plural = _("skill and Japanese language qualifications")
        ordering = ["area", "passed_on", "pk"]

    def __str__(self) -> str:
        return f"{self.worker}: {self.get_area_display()}"


class InternshipCompletion(HistoryRecord):
    """良好に修了した技能実習2号（職種・作業・証明。T46a）。"""

    worker = models.ForeignKey(
        ForeignWorker,
        verbose_name=_("foreign worker"),
        on_delete=models.PROTECT,
        related_name="internship_completions",
    )
    occupation = models.CharField(_("job category"), max_length=3)  # CC02_3
    work = models.CharField(_("operation"), max_length=2)  # CB30（職種ごとの連動の表）
    proof = models.CharField(_("proof of good completion"), max_length=2)  # CC07
    completed_on = models.DateField(_("completed on"), null=True, blank=True)
    attachments = GenericRelation(Attachment)

    class Meta:
        verbose_name = _("completed Technical Intern Training (ii)")
        verbose_name_plural = _("completed Technical Intern Training (ii)")
        ordering = ["completed_on", "pk"]

    def __str__(self) -> str:
        return f"{self.worker}: {self.occupation}/{self.work}"


class WorkHistory(HistoryRecord):
    """職歴（入社・退社の年月と勤務先。一括申請は 10 件まで。T46a）。年月は月の 1 日で持つ。"""

    worker = models.ForeignKey(
        ForeignWorker,
        verbose_name=_("foreign worker"),
        on_delete=models.PROTECT,
        related_name="work_history",
    )
    joined_on = models.DateField(_("joined (year and month)"))
    left_on = models.DateField(_("left (year and month)"), null=True, blank=True)
    employer = models.CharField(_("employer"), max_length=60)

    class Meta:
        verbose_name = _("work history")
        verbose_name_plural = _("work history")
        ordering = ["joined_on", "pk"]

    def __str__(self) -> str:
        return f"{self.worker}: {self.employer}"


# --- 最新行（DB ビュー。migrations/0002 で作成。読み取り専用） -----------------------------


class LatestResidenceRecord(models.Model):
    worker = models.OneToOneField(
        ForeignWorker,
        primary_key=True,
        on_delete=models.DO_NOTHING,
        related_name="latest_residence",
    )
    record = models.ForeignKey(ResidenceRecord, on_delete=models.DO_NOTHING, related_name="+")

    class Meta:
        managed = False
        db_table = "workers_latest_residence"

    def __str__(self) -> str:
        return f"{self.worker_id}: {self.record_id}"


class LatestPassportRecord(models.Model):
    worker = models.OneToOneField(
        ForeignWorker,
        primary_key=True,
        on_delete=models.DO_NOTHING,
        related_name="latest_passport",
    )
    record = models.ForeignKey(PassportRecord, on_delete=models.DO_NOTHING, related_name="+")

    class Meta:
        managed = False
        db_table = "workers_latest_passport"

    def __str__(self) -> str:
        return f"{self.worker_id}: {self.record_id}"


class LatestWorkerAddress(models.Model):
    worker = models.OneToOneField(
        ForeignWorker,
        primary_key=True,
        on_delete=models.DO_NOTHING,
        related_name="latest_address",
    )
    record = models.ForeignKey(WorkerAddress, on_delete=models.DO_NOTHING, related_name="+")

    class Meta:
        managed = False
        db_table = "workers_latest_address"

    def __str__(self) -> str:
        return f"{self.worker_id}: {self.record_id}"


class LatestWorkerContact(models.Model):
    worker = models.OneToOneField(
        ForeignWorker,
        primary_key=True,
        on_delete=models.DO_NOTHING,
        related_name="latest_contact",
    )
    record = models.ForeignKey(WorkerContact, on_delete=models.DO_NOTHING, related_name="+")

    class Meta:
        managed = False
        db_table = "workers_latest_contact"

    def __str__(self) -> str:
        return f"{self.worker_id}: {self.record_id}"
