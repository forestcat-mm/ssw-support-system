"""届出・申請タスク（仕様書第5章 F07、第7章 filings）。

期限は T10 の Deadline を保存する（起票した時点の計算結果。起算日を直したら計算し直す）。
種別ごとの区別（入管の届出／入管の申請／内部）と届出主体は
services.filings.KIND_DEFINITIONS に持つ。
状態の遷移は services.filings.transition に集約する。
"""

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.core.models import Staff, TrackedModel
from apps.workers.models import ForeignWorker


class FilingKind(models.TextChoices):
    RESIDENCE_RENEWAL = "residence_renewal", _("application for extension of period of stay")
    CONTRACT_CHANGE = "contract_change", _("notification of change of employment contract")
    CONTRACT_TERMINATION = (
        "contract_termination",
        _("notification of termination of employment contract"),
    )
    CONTRACT_NEW = "contract_new", _("notification of conclusion of a new employment contract")
    ACCEPTANCE_DIFFICULTY = "acceptance_difficulty", _("notification of difficulty in acceptance")
    SUPPORT_PLAN_CHANGE = "support_plan_change", _("notification of change of support plan")
    MISSING = "missing", _("notification of missing worker, etc.")
    SUPPORT_DIFFICULTY = (
        "support_difficulty",
        _("notification of difficulty in implementing support (form 3-7)"),
    )
    PERIODIC_HOST = "periodic_host", _("periodic notification (host organization)")
    PERIODIC_SUPPORT_ORGANIZATION = (
        "periodic_support_organization",
        _("periodic notification (registered support organization)"),
    )
    REGISTRATION_RENEWAL = "registration_renewal", _("renewal of registration (own organization)")
    MEMBERSHIP_RENEWAL = (
        "membership_renewal",
        _("renewal of membership certificate or cooperation confirmation"),
    )
    PASSPORT_CHECK = "passport_check", _("check of passport renewal")
    SUPPORT_CONTRACT_RENEWAL = "support_contract_renewal", _("renewal of support contract")
    CONTRACT_EXPIRY_CHECK = (
        "contract_expiry_check",
        _("check of employment contract expiry (termination not registered)"),
    )


class FilingStatus(models.TextChoices):
    NOT_STARTED = "not_started", _("not started")
    IN_PROGRESS = "in_progress", _("in progress")
    SUBMITTED = "submitted", _("submitted")
    ACCEPTED = "accepted", _("accepted")
    NOT_REQUIRED = "not_required", _("not required")
    CANCELLED = "cancelled", _("cancelled")


class Trigger(models.TextChoices):
    CONTRACT_TERMINATED = "contract_terminated", _("employment contract terminated")
    CONTRACT_CHANGED = "contract_changed", _("employment contract changed")
    CONTRACT_CONCLUDED = "contract_concluded", _("new employment contract concluded")
    RESIDENCE_EXPIRY = "residence_expiry", _("period of stay approaching expiry")
    PERIODIC_SEASON = "periodic_season", _("periodic notification period")
    REGISTRATION_EXPIRY = "registration_expiry", _("registration renewal period opened")
    DOCUMENT_EXPIRY = "document_expiry", _("document approaching expiry")
    PASSPORT_EXPIRY = "passport_expiry", _("passport approaching expiry")
    SUPPORT_CONTRACT_END = "support_contract_end", _("support contract approaching end")
    CONTRACT_EXPIRY = "contract_expiry", _("employment contract period approaching end")
    MANUAL = "manual", _("registered manually")


class Filing(TrackedModel):
    """届出・申請タスク。同じ種別×対象×起算日は 1 件（取消を除く）。削除しない。"""

    kind = models.CharField(_("kind"), max_length=40, choices=FilingKind.choices)
    content_type = models.ForeignKey(
        ContentType, verbose_name=_("target type"), on_delete=models.PROTECT
    )
    object_id = models.PositiveBigIntegerField(_("target ID"))
    target = GenericForeignKey("content_type", "object_id")
    target_repr = models.CharField(_("target"), max_length=255)
    worker = models.ForeignKey(
        ForeignWorker,
        verbose_name=_("foreign worker"),
        on_delete=models.PROTECT,
        related_name="filings",
        null=True,
        blank=True,
    )
    trigger = models.CharField(_("trigger"), max_length=32, choices=Trigger.choices)
    trigger_details = models.JSONField(
        _("trigger details"), default=dict, blank=True, encoder=DjangoJSONEncoder
    )
    needs_review = models.BooleanField(
        _("needs review of whether filing is required"), default=False
    )

    # 期限（T10 の Deadline）
    deadline_kind = models.CharField(_("deadline kind"), max_length=40)
    basis_on = models.DateField(_("basis date"))
    opens_on = models.DateField(_("opens on"), null=True, blank=True)
    due_on = models.DateField(_("legal due date"))
    unadjusted_due_on = models.DateField(_("due date (before holiday adjustment)"))
    final_on = models.DateField(_("final deadline"), null=True, blank=True)

    status = models.CharField(
        _("status"), max_length=16, choices=FilingStatus.choices, default=FilingStatus.NOT_STARTED
    )
    status_reason = models.TextField(_("reason"), blank=True)
    submitted_on = models.DateField(_("submitted on"), null=True, blank=True)
    receipt_number = models.CharField(_("receipt number"), max_length=50, blank=True)
    assignee = models.ForeignKey(
        Staff,
        verbose_name=_("assignee"),
        on_delete=models.PROTECT,
        related_name="filings",
        null=True,
        blank=True,
    )
    note = models.TextField(_("note"), blank=True)
    # 生成した帳票への参照は段階3（GeneratedDocument）で追加する

    class Meta:
        verbose_name = _("filing task")
        verbose_name_plural = _("filing tasks")
        ordering = ["unadjusted_due_on", "pk"]
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
            models.Index(fields=["status", "unadjusted_due_on"]),
        ]
        constraints = [
            # 重複起票しない（取り消した後に同じ事由が起きたら新しく起票できる）
            models.UniqueConstraint(
                fields=["kind", "content_type", "object_id", "basis_on"],
                condition=~Q(status="cancelled"),
                name="filings_filing_unique_open",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()}: {self.target_repr} ({self.due_on})"
