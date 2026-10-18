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

from django.core.exceptions import ValidationError
from django.core.files import File
from django.db import models, transaction
from django.utils.translation import gettext as _

from apps.core.models import Attachment, AttachmentKind, User
from apps.hosts.models import (
    HostCooperationConfirmation,
    HostMembership,
    HostOrganization,
    HostSite,
    OperationCategory,
    SupportContract,
)
from services import attachments, corporate_number, filings, hosts

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
    site: HostSite, categories: Iterable[OperationCategory], *, by: User | None
) -> SiteSaved:
    categories = list(categories)
    hosts.validate_site_categories(site.field, categories)
    hosts.validate_site_change(
        site, field=site.field, categories=categories, is_active=site.is_active
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


@transaction.atomic
def save_support_contract(
    contract: SupportContract,
    *,
    files: Iterable[File[Any]] = (),
    by: User | None,
    today: datetime.date,
) -> filings.SyncResult:
    """1 行＝1 期間。自動更新の後の期間は次の行で登録する（ADR-0015）。"""
    hosts.validate_contract_period(
        contract.host_organization_id,
        contract.starts_on,
        contract.ends_on,
        exclude_pk=contract.pk,
    )
    files = list(files)
    _validate_files(AttachmentKind.SUPPORT_CONTRACT, files)
    _stamp(contract, by)
    contract.save()
    for file in files:
        attachments.create_attachment(
            target=contract, kind=AttachmentKind.SUPPORT_CONTRACT, file=file, created_by=by
        )
    return filings.sync_support_contracts(contract.host_organization, by=by, today=today)


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
