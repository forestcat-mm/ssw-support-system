"""様式マスタの管理（F13。T53、ADR-0042）。

- 版の一覧（様式 ID ごと。適用開始日・出典・SHA-256・生成の件数・停止の状態）。
- 版の停止と解除（T53-D2）: 不備のある版を基準日の自動の選択から外し、前の版に戻す。理由は必須で、
  監査ログに残す。同じ様式 ID・言語の版がほかに無い（停止していない）ときは停止できない。
- 別の版で作り直す（T53-D2）: 生成物のスナップショット（生成のときのデータ）で、選んだ版の新しい
  生成履歴を作る（旧版での再生成）。

新しい版の登録はリポジトリのデータ（templates/forms・mappings）と `make forms` を正にする（D8）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from django.core.exceptions import ValidationError
from django.db.models import Count
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.models import AuditAction, User
from apps.forms_engine.models import FormOwner, FormTemplate, GeneratedDocument
from services import audit, form_generation


@dataclass
class FormGroup:
    form_id: str
    name: str
    versions: list[FormTemplate] = field(default_factory=list)


def groups() -> list[FormGroup]:
    """様式 ID ごとの版（新しい順）。共通の版だけ（受入れ機関固有の版は持たない）。"""
    templates = (
        FormTemplate.objects.filter(owner=FormOwner.COMMON)
        .select_related("mapping")
        .annotate(documents=Count("generated_documents"))
        .order_by("form_id", "-valid_from", "language")
    )
    by_id: dict[str, FormGroup] = {}
    for template in templates:
        group = by_id.setdefault(template.form_id, FormGroup(template.form_id, template.name))
        group.versions.append(template)
    return sorted(by_id.values(), key=lambda group: _sort_key(group.form_id))


def _sort_key(form_id: str) -> tuple[Any, ...]:
    parts = form_id.split("-")
    return tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in parts)


def siblings(template: FormTemplate) -> list[FormTemplate]:
    """同じ様式 ID・言語の共通の版（新しい順）。"""
    return list(
        FormTemplate.objects.filter(
            form_id=template.form_id, language=template.language, owner=FormOwner.COMMON
        ).order_by("-valid_from")
    )


def suspend(template: FormTemplate, *, reason: str, by: User | None) -> None:
    reason = reason.strip()
    if not reason:
        raise ValidationError({"reason": _("Enter the reason.")})
    if template.suspended_at is not None:
        return
    others = [
        other
        for other in siblings(template)
        if other.pk != template.pk and other.suspended_at is None
    ]
    if not others:
        raise ValidationError(
            _("This is the only version in use. Load a corrected version before suspending it.")
        )
    with audit.event(AuditAction.UPDATE, target=template, actor=by, context={"suspended": reason}):
        template.suspended_at = timezone.now()
        template.suspended_reason = reason[:200]
        template.updated_by = by
        template.save(
            update_fields=["suspended_at", "suspended_reason", "updated_by", "updated_at"]
        )


def resume(template: FormTemplate, *, by: User | None) -> None:
    if template.suspended_at is None:
        return
    with audit.event(AuditAction.UPDATE, target=template, actor=by, context={"resumed": True}):
        template.suspended_at = None
        template.suspended_reason = ""
        template.updated_by = by
        template.save(
            update_fields=["suspended_at", "suspended_reason", "updated_by", "updated_at"]
        )


def regenerate(
    document: GeneratedDocument, template: FormTemplate, *, by: User | None
) -> GeneratedDocument:
    """生成物のデータ（スナップショット）で、選んだ版の新しい生成履歴を作る（旧版での再生成）。"""
    if (template.form_id, template.language) != (
        document.template.form_id,
        document.template.language,
    ):
        raise ValidationError(_("Choose a version of the same form."))
    snapshot = json.loads(document.snapshot)
    target = document.target
    if target is None:
        raise ValidationError(_("The target of this form no longer exists."))
    return form_generation.request(
        template.form_id,
        target=target,
        data=snapshot["data"],
        basis_on=document.basis_on,
        by=by,
        language=template.language,
        accept_warnings=True,
        template=template,
    )
