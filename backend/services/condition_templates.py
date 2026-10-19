"""雇用条件のひな形（T41、ADR-0034）。

- ひな形は受入れ機関ごと。版の入力（第1-6号の項目と、交代制・手当・控除の行）の値を JSON で持つ。
  変更日・変更理由・締結日は契約ごとの値なので持たない。
- 外国人ごとに入れる項目に印（marked）を付ける。契約の登録の画面でひな形を選ぶと、印の無い項目に
  値が入り、印の付いた項目は空のまま画面の先頭にまとめて出す（T41-D2）。
- ひな形は値を写すだけで、版とは結ばない（T41-D3）。版の保存・検査・届出の判定は今のとおり
  （contracts.create_version）。ひな形を直しても、作成済みの版は変わらない。
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext as _

from apps.contracts.models import EmploymentConditionTemplate
from apps.core.models import User
from apps.hosts.models import HostOrganization, HostSite
from services import contracts

# ひな形に持たない項目（契約ごとの値）
EXCLUDED = frozenset({"changed_on", "change_reason", "signed_on"})
DETAIL_KINDS = ("shifts", "allowances", "deductions")


def to_json(value: Any) -> Any:
    """フォームの値を JSON にする（日付は ISO、時刻は HH:MM、数は文字、モデルは ID）。"""
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, datetime.time):
        return value.strftime("%H:%M")
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, models.Model):
        return value.pk
    if isinstance(value, models.QuerySet) or isinstance(value, (list, tuple)):
        return [to_json(item) for item in value]
    return value


def is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == ()


def values_from(cleaned: Mapping[str, Any], names: Iterable[str]) -> dict[str, Any]:
    """版のフォームの cleaned_data から、ひな形の値（空の項目は持たない）。"""
    found: dict[str, Any] = {}
    for name in names:
        if name in EXCLUDED or name not in cleaned:
            continue
        value = cleaned[name]
        if is_empty(value) or (isinstance(value, models.QuerySet) and not value.exists()):
            continue
        found[name] = to_json(value)
    return found


def details_from(details: contracts.Details) -> dict[str, list[dict[str, Any]]]:
    """明細の行（保存前のモデル）から、ひな形の行。"""
    fields = {
        "shifts": ("start_time", "end_time", "applies_to", "daily_working_minutes"),
        "allowances": ("name", "amount", "calculation_method", "fixed_overtime_hours"),
        "deductions": ("kind", "name", "amount"),
    }
    rows = {"shifts": details.shifts, "allowances": details.allowances}
    rows["deductions"] = details.deductions
    return {
        kind: [{name: to_json(getattr(row, name)) for name in fields[kind]} for row in rows[kind]]
        for kind in DETAIL_KINDS
    }


def save_template(
    template: EmploymentConditionTemplate,
    *,
    values: Mapping[str, Any],
    details: Mapping[str, Sequence[Mapping[str, Any]]],
    marked: Iterable[str],
    allowed: Iterable[str],
    by: User | None,
) -> EmploymentConditionTemplate:
    """ひな形を保存する（事業所は受入れ機関のもの、印は版の項目か明細の行だけ）。"""
    allowed_names = set(allowed) | set(DETAIL_KINDS)
    marks = sorted(set(marked))
    errors: dict[str, list[ValidationError]] = {}
    unknown = [name for name in marks if name not in allowed_names or name in EXCLUDED]
    if unknown:
        errors["marked"] = [ValidationError(_("Unknown items are marked."), code="unknown_mark")]
    site_id = values.get("site")
    if (
        site_id is not None
        and not HostSite.objects.filter(
            pk=site_id, host_organization=template.host_organization
        ).exists()
    ):
        errors["site"] = [
            ValidationError(
                _("Select a site of the host organization of the template."), code="site"
            )
        ]
    duplicate = EmploymentConditionTemplate.objects.filter(
        host_organization=template.host_organization, name=template.name
    ).exclude(pk=template.pk)
    if duplicate.exists():
        errors["name"] = [
            ValidationError(_("A template with this name already exists."), code="duplicate")
        ]
    if errors:
        raise ValidationError(errors)
    template.values = dict(values)
    template.details = {kind: [dict(row) for row in details.get(kind, [])] for kind in DETAIL_KINDS}
    template.marked = marks
    template.updated_by = by
    if template.pk is None:
        template.created_by = by
    template.save()
    return template


def set_active(template: EmploymentConditionTemplate, active: bool, *, by: User | None) -> None:
    template.is_active = active
    template.updated_by = by
    template.save(update_fields=["is_active", "updated_by", "updated_at"])


def available() -> list[EmploymentConditionTemplate]:
    """契約の登録で選べるひな形（使う・受入れ機関が有効）。"""
    return list(
        EmploymentConditionTemplate.objects.filter(
            is_active=True, host_organization__is_active=True
        )
        .select_related("host_organization")
        .order_by("host_organization__name", "name", "pk")
    )


def of_host(host: HostOrganization) -> list[EmploymentConditionTemplate]:
    return list(host.condition_templates.order_by("-is_active", "name", "pk"))


def initial_for(
    template: EmploymentConditionTemplate,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """契約の登録の初期値（印の無い項目だけ）と、明細の行（印の無い種類だけ）。"""
    marked = set(template.marked)
    initial = {
        name: value
        for name, value in template.values.items()
        if name not in marked and name not in EXCLUDED
    }
    rows = {
        kind: [dict(row) for row in template.details.get(kind, [])]
        for kind in DETAIL_KINDS
        if kind not in marked
    }
    return initial, rows


def edit_initial(
    template: EmploymentConditionTemplate,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """ひな形の編集の画面の初期値（全部の項目と行）。"""
    rows = {kind: [dict(row) for row in template.details.get(kind, [])] for kind in DETAIL_KINDS}
    return dict(template.values), rows
