"""受入れ機関の登録・編集（業務画面。第5章 F02、T28a）。

- 検査は services.hosts（業務区分・事業所の編集の制限・支援委託契約の期間）と
  services.corporate_number。画面には書かない。
- 加入状況と支援委託契約は、保存のときに期限のタスクを片付けて起票する（services.filings の
  sync_*。日次の起票と同じタスクなので二重にはならない）。
- 証明書の差し替えは添付を足すだけで、古い証明書は残す（自機関の構成員証明書と同じ）。
- 添付の誤りは取消（理由必須。ファイルは残す。services.attachments.void_attachment）。
- 作成・更新・添付は保存のシグナルで監査ログに残る（AUDITED_MODELS）。
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.files import File
from django.db import models, transaction
from django.utils.translation import gettext as _

from apps.core.models import Attachment, AttachmentKind, AuditAction, AuditLog, User
from apps.filings.models import Filing, FilingKind, FilingStatus
from apps.hosts.models import (
    HostCooperationConfirmation,
    HostMembership,
    HostOrganization,
    HostSite,
    OperationCategory,
    SupportContract,
)
from services import attachments, audit, corporate_number, filings, hosts, support_filings

# 添付できる対象と種別（ほかの種別では添付させない）
ATTACHMENT_KINDS: dict[type[models.Model], AttachmentKind] = {
    HostMembership: AttachmentKind.MEMBERSHIP_CERTIFICATE,
    HostCooperationConfirmation: AttachmentKind.COOPERATION_CONFIRMATION,
    SupportContract: AttachmentKind.SUPPORT_CONTRACT,
}


def _stamp(instance: Any, by: User | None) -> None:
    if instance.pk is None:
        instance.created_by = by
    instance.updated_by = by


def host_of(target: models.Model) -> HostOrganization:
    """添付の対象（加入状況・協力確認書・支援委託契約）の受入れ機関。"""
    host = getattr(target, "host_organization", None)
    assert isinstance(host, HostOrganization)
    return host


# --- 受入れ機関 --------------------------------------------------------------------


def save_host(host: HostOrganization, *, by: User | None) -> HostOrganization:
    host.corporate_number = corporate_number.normalize(host.corporate_number)
    corporate_number.validate(host.corporate_number)
    hosts.validate_corporate_number_for(host.entity_type, host.corporate_number)
    _stamp(host, by)
    host.save()
    return host


# --- 事業所 -----------------------------------------------------------------------


@dataclass(frozen=True)
class SiteSaved:
    site: HostSite
    # 所在地を変えた事業所に協力確認書の提出があれば、出し直しが要る（運用要領 本体 印刷 81頁）
    resubmission_needed: bool


@transaction.atomic
def save_site(
    site: HostSite,
    categories: Iterable[OperationCategory],
    *,
    by: User | None,
    today: datetime.date,
) -> SiteSaved:
    categories = list(categories)
    hosts.validate_site_categories(site.field, categories)
    hosts.validate_site_change(
        site, field=site.field, categories=categories, is_active=site.is_active, today=today
    )
    moved = False
    if site.pk is not None:
        saved = HostSite.objects.get(pk=site.pk)
        moved = (saved.postal_code, saved.address) != (site.postal_code, site.address)
    _stamp(site, by)
    site.save()
    site.categories.set(categories)
    return SiteSaved(
        site=site, resubmission_needed=moved and site.cooperation_confirmations.exists()
    )


# --- 加入状況 ---------------------------------------------------------------------


@transaction.atomic
def save_membership(
    membership: HostMembership, *, by: User | None, today: datetime.date
) -> filings.SyncResult:
    if membership.pk is not None:
        saved = HostMembership.objects.get(pk=membership.pk)
        if saved.field_id != membership.field_id:
            raise ValidationError(
                {"field": _("The field of a membership cannot be changed after registration.")}
            )
    _stamp(membership, by)
    membership.save()
    return filings.sync_membership(membership, by=by, today=today)


@transaction.atomic
def replace_membership_certificate(
    membership: HostMembership,
    *,
    file: File[Any],
    valid_until: datetime.date | None,
    by: User | None,
    today: datetime.date,
) -> filings.SyncResult:
    """証明書を差し替える（古い証明書は残る）。加入状況の有効期限も新しい証明書に合わせる。"""
    attachments.create_attachment(
        target=membership,
        kind=AttachmentKind.MEMBERSHIP_CERTIFICATE,
        file=file,
        created_by=by,
        valid_until=valid_until,
    )
    if membership.valid_until != valid_until:
        membership.valid_until = valid_until
        membership.updated_by = by
        membership.save()
    return filings.sync_membership(membership, by=by, today=today, certificate_replaced=True)


# --- 協力確認書 --------------------------------------------------------------------


@transaction.atomic
def save_cooperation(
    confirmation: HostCooperationConfirmation,
    *,
    files: Iterable[File[Any]] = (),
    by: User | None,
) -> HostCooperationConfirmation:
    """協力確認書の提出を保存する（再提出は新しい行。提出先と事業所の所在地の一致は見ない）。"""
    hosts.validate_cooperation_site(confirmation.host_organization_id, confirmation.site)
    files = list(files)
    _validate_files(AttachmentKind.COOPERATION_CONFIRMATION, files)
    _stamp(confirmation, by)
    confirmation.save()
    for file in files:
        attachments.create_attachment(
            target=confirmation,
            kind=AttachmentKind.COOPERATION_CONFIRMATION,
            file=file,
            created_by=by,
        )
    return confirmation


# --- 支援委託契約 ------------------------------------------------------------------


@dataclass(frozen=True)
class SupportSaved:
    """支援委託契約の保存の結果（期限のタスクの処理と、届出タスクの起票）。"""

    sync: filings.SyncResult
    raised: support_filings.Raised


@dataclass(frozen=True)
class Amendment:
    """同じ行の委託料・期間を「契約の変更」として直す（誤りの訂正なら None）。"""

    changed_on: datetime.date


AMENDABLE_FIELDS = ("monthly_fee", "starts_on", "ends_on")


@transaction.atomic
def save_support_contract(
    contract: SupportContract,
    *,
    files: Iterable[File[Any]] = (),
    by: User | None,
    today: datetime.date,
    amendment: Amendment | None = None,
) -> SupportSaved:
    """1 行＝1 期間。自動更新の後の期間は次の行で登録する（ADR-0015・0017）。

    新しい行は締結（続く行なら委託料の変更だけ）として届出タスクを起票する。既存の行の委託料・
    期間の変更は、amendment があれば第3-3-1号を起票し、無ければ誤りの訂正として起票しない。
    """
    hosts.validate_contract_period(
        contract.host_organization_id,
        contract.starts_on,
        contract.ends_on,
        exclude_pk=contract.pk,
        terminated_on=contract.terminated_on,
    )
    changes: dict[str, list[Any]] = {}
    is_new = contract.pk is None
    if not is_new:
        saved = SupportContract.objects.get(pk=contract.pk)
        changes = {
            name: [getattr(saved, name), getattr(contract, name)]
            for name in AMENDABLE_FIELDS
            if getattr(saved, name) != getattr(contract, name)
        }
    files = list(files)
    _validate_files(AttachmentKind.SUPPORT_CONTRACT, files)
    _stamp(contract, by)
    contract.save()
    for file in files:
        attachments.create_attachment(
            target=contract, kind=AttachmentKind.SUPPORT_CONTRACT, file=file, created_by=by
        )
    sync = filings.sync_support_contracts(contract.host_organization, by=by, today=today)
    if is_new:
        raised = support_filings.on_registered(contract, by=by)
    elif changes and amendment is not None:
        raised = support_filings.on_amended(
            contract, changes=changes, on=amendment.changed_on, by=by
        )
    else:
        raised = support_filings.Raised()
    return SupportSaved(sync=sync, raised=raised)


@transaction.atomic
def terminate_support_contract(
    contract: SupportContract,
    *,
    terminated_on: datetime.date | None,
    category: str,
    reason: str,
    note: str,
    by: User | None,
    today: datetime.date,
) -> SupportSaved:
    """終了を登録する（実際の終了日と事由。予定の終了日は残す。ADR-0017）。"""
    hosts.validate_termination(
        contract, terminated_on=terminated_on, category=category, reason=reason, note=note
    )
    contract.terminated_on = terminated_on
    contract.termination_category = category
    contract.termination_reason = reason
    contract.termination_note = note.strip()
    contract.updated_by = by
    contract.save()
    sync = filings.sync_support_contracts(contract.host_organization, by=by, today=today)
    raised = support_filings.on_terminated(contract, by=by)
    return SupportSaved(sync=sync, raised=raised)


@transaction.atomic
def cancel_support_termination(
    contract: SupportContract, *, reason: str, by: User | None, today: datetime.date
) -> SupportSaved:
    """終了の登録を取り消す（入力の誤りの訂正。管理者だけ。理由は必須）。

    起票した届出タスクは、未着手なら理由つきで取消、対応中・完了などは残す（T28b と同じ考え方）。
    終了の登録で自動で完了にした更新のタスクは、未着手に戻す。
    """
    if not reason.strip():
        raise ValidationError({"reason": _("Enter the reason.")})
    terminated_on = contract.terminated_on
    if terminated_on is None:
        raise ValidationError(_("The termination is not registered."))
    hosts.validate_contract_period(
        contract.host_organization_id,
        contract.starts_on,
        contract.ends_on,
        exclude_pk=contract.pk,
    )
    with audit.event(
        AuditAction.UPDATE, target=contract, actor=by, context={"cancel_termination": reason}
    ):
        contract.terminated_on = None
        contract.termination_category = ""
        contract.termination_reason = ""
        contract.termination_note = ""
        contract.updated_by = by
        contract.save()
    raised = support_filings.on_termination_cancelled(
        contract, terminated_on=terminated_on, reason=reason, by=by
    )
    _reopen_renewal_closed_by_termination(contract, reason=reason, by=by)
    sync = filings.sync_support_contracts(contract.host_organization, by=by, today=today)
    return SupportSaved(sync=sync, raised=raised)


def _reopen_renewal_closed_by_termination(
    contract: SupportContract, *, reason: str, by: User | None
) -> None:
    """終了の登録で自動で完了にした「支援委託契約の更新」のタスクを未着手に戻す。"""
    filing_type = ContentType.objects.get_for_model(Filing)
    for filing in Filing.objects.filter(
        kind=FilingKind.SUPPORT_CONTRACT_RENEWAL,
        content_type=ContentType.objects.get_for_model(SupportContract),
        object_id=contract.pk,
        status=FilingStatus.COMPLETED,
    ):
        last = (
            AuditLog.objects.filter(
                action=AuditAction.FILING_TRANSITION,
                content_type=filing_type,
                object_id=str(filing.pk),
            )
            .order_by("-pk")
            .first()
        )
        if last is None or not last.context.get("automatic"):
            continue
        if last.context.get("source") != f"hosts.supportcontract:{contract.pk}":
            continue  # 次の行の登録で完了にしたもの（終了の登録ではない）
        filings.transition(
            filing,
            FilingStatus.NOT_STARTED,
            by=by,
            reason=_("The termination of the support contract was cancelled: %(reason)s")
            % {"reason": reason},
            context={"automatic": True, "source": f"hosts.supportcontract:{contract.pk}"},
        )


# --- 添付 ------------------------------------------------------------------------


def _validate_files(kind: AttachmentKind, files: list[File[Any]]) -> None:
    messages: list[str] = []
    for file in files:
        try:
            attachments.validate_upload(kind, file)
        except ValidationError as error:
            messages.extend(error.messages)
    if messages:
        raise ValidationError({"uploads": messages})


def kind_for(target: models.Model) -> AttachmentKind:
    return ATTACHMENT_KINDS[type(target)]


@transaction.atomic
def add_attachments(
    target: models.Model, files: Iterable[File[Any]], *, by: User | None
) -> list[Attachment]:
    """加入状況以外（協力確認書の控え・支援委託契約書）に添付する。証明書は差し替えで足す。"""
    kind = kind_for(target)
    files = list(files)
    if not files:
        raise ValidationError({"uploads": _("Select a file.")})
    _validate_files(kind, files)
    return [
        attachments.create_attachment(target=target, kind=kind, file=file, created_by=by)
        for file in files
    ]


def attachments_of_host(host: HostOrganization) -> models.QuerySet[Attachment]:
    """受入れ機関の加入状況・協力確認書・支援委託契約の添付（取消の画面の対象を絞る）。"""
    query = models.Q()
    for model, related in (
        (HostMembership, host.memberships),
        (HostCooperationConfirmation, host.cooperation_confirmations),
        (SupportContract, host.support_contracts),
    ):
        query |= models.Q(
            content_type__app_label=model._meta.app_label,
            content_type__model=model._meta.model_name,
            object_id__in=list(related.values_list("pk", flat=True)),
            kind=ATTACHMENT_KINDS[model],
        )
    return Attachment.objects.filter(query).select_related("content_type")
