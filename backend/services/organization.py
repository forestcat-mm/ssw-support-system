"""自機関（1 行）の取得と、設定画面（T15b）からの自機関・事務所・担当者の保存。"""

from datetime import date
from typing import Any

from django.core.exceptions import ValidationError
from django.core.files import File
from django.db import transaction
from django.db.models import Model, QuerySet
from django.utils.translation import gettext as _

from apps.core.models import Attachment, AttachmentKind, Office, Organization, Staff, User
from services import attachments


class OrganizationNotConfiguredError(Exception):
    """自機関がまだ登録されていない。"""


DEFAULT_DISPLAY_NAME = "SSW Support"


def organization_display_name() -> str:
    """画面・メール・認証アプリに出す自機関名（未登録時は既定名）。"""
    try:
        return get_organization().name
    except OrganizationNotConfiguredError:
        return DEFAULT_DISPLAY_NAME


def get_organization() -> Organization:
    organization = Organization.objects.first()
    if organization is None:
        raise OrganizationNotConfiguredError("Organization has not been registered yet.")
    return organization


def save_record(instance: Model, *, by: User | None) -> Model:
    """自機関・事務所・担当者を保存する（作成者・更新者を入れる。監査ログは保存のシグナルで残る）。"""
    if instance.pk is None:
        instance.created_by = by  # type: ignore[attr-defined]
    instance.updated_by = by  # type: ignore[attr-defined]
    instance.save()
    return instance


@transaction.atomic
def replace_council_certificate(
    organization: Organization, *, file: File[Any], valid_until: date | None, by: User | None
) -> Attachment:
    """食品産業特定技能協議会の構成員証明書を差し替える（古い証明書は消さずに残る）。"""
    return attachments.create_attachment(
        target=organization,
        kind=AttachmentKind.FOOD_COUNCIL_CERTIFICATE,
        file=file,
        created_by=by,
        valid_until=valid_until,
    )


def council_certificates(organization: Organization) -> list[Attachment]:
    """構成員証明書（新しい順）。"""
    return list(
        organization.attachments.filter(kind=AttachmentKind.FOOD_COUNCIL_CERTIFICATE).order_by(
            "-created_at", "-pk"
        )
    )


# --- 事務所の支援責任者（第1-17号 Ⅲ欄７。修正 1 回目 A5、ADR-0028）-----------------------


def manager_choices(office: Office | None) -> QuerySet[Staff]:
    """事務所の支援責任者に選べる担当者（その事務所の、支援責任者の印のある有効な担当者）。"""
    if office is None or office.pk is None:
        return Staff.objects.none()
    return Staff.objects.filter(office=office, is_support_manager=True, is_active=True).order_by(
        "name_kana", "name"
    )


def validate_support_manager(office: Office, staff: Staff | None) -> None:
    if staff is None:
        return
    if not manager_choices(office).filter(pk=staff.pk).exists():
        raise ValidationError(
            _("Choose an active staff member of this office who is marked as a support manager."),
            code="manager",
        )


def support_manager(office: Office | None) -> Staff | None:
    """事務所の支援責任者（選ばれていて、今も条件に合う人だけ）。"""
    if office is None or office.support_manager_id is None:
        return None
    manager = office.support_manager
    if manager is None or not manager_choices(office).filter(pk=manager.pk).exists():
        return None
    return manager
