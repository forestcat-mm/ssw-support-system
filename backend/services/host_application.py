"""受入れ機関・自機関の一括申請の項目の保存（決算・申告の版・役員・申請担当者。T46b、ADR-0038）。

決算と申告は上書きしない（取消〔理由は必須〕して新しい行・新しい版）。役員は退任日を入れて残す。
"""

from typing import Any

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.models import User, VoidableRecord
from apps.hosts.models import HostDeclaration


def _stamp(instance: Any, by: User | None) -> None:
    if instance.pk is None:
        instance.created_by = by
    instance.updated_by = by


def save_row(row: models.Model, *, by: User | None) -> models.Model:
    """決算・役員・申請担当者の行を保存する（作成・更新は監査ログに残る）。"""
    _stamp(row, by)
    row.save()
    return row


def add_declaration(
    declaration: HostDeclaration, answers: dict[str, dict[str, str]], *, by: User | None
) -> HostDeclaration:
    """申告の新しい版（前の版は残す）。"""
    if declaration.pk is not None:
        raise ValueError("a declaration is not changed; add a new version")
    declaration.answers = answers
    _stamp(declaration, by)
    declaration.save()
    return declaration


def void_row(row: VoidableRecord, *, by: User | None, reason: str) -> None:
    if row.voided_at is not None:
        return
    if not reason.strip():
        raise ValidationError(_("Enter the reason for voiding."), code="void_reason")
    row.voided_at = timezone.now()
    row.voided_by = by
    row.void_reason = reason.strip()[:200]
    row.updated_by = by
    row.save(update_fields=["voided_at", "voided_by", "void_reason", "updated_by", "updated_at"])
