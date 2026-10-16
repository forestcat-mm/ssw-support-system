"""受入れ機関の業務ロジック（業務区分の検査・加入状況・支援委託契約の数）。"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils.translation import gettext as _

from apps.hosts.models import (
    HostMembership,
    HostOrganization,
    OperationCategory,
    SpecifiedSkillField,
    SupportContract,
)


def validate_site_categories(
    field: SpecifiedSkillField | None, categories: Iterable[OperationCategory]
) -> None:
    """事業所の業務区分は、事業所の分野に属するものだけ。"""
    others = [c for c in categories if field is None or c.field_id != field.pk]
    if others:
        raise ValidationError(
            _("These operation categories do not belong to the site's field: %(names)s")
            % {"names": ", ".join(str(c) for c in others)},
            code="category_field",
        )


def membership_on(
    host: HostOrganization, field: SpecifiedSkillField, on: date
) -> HostMembership | None:
    """その日に有効な加入状況（加入日が on 以前で、有効期限が空または on 以後）。"""
    return (
        host.memberships.filter(field=field, joined_on__lte=on)
        .filter(Q(valid_until__isnull=True) | Q(valid_until__gte=on))
        .first()
    )


def is_member(host: HostOrganization, field: SpecifiedSkillField, on: date) -> bool:
    return membership_on(host, field, on) is not None


def validate_contract_period(
    host_id: int | None,
    starts_on: date | None,
    ends_on: date | None,
    *,
    exclude_pk: int | None = None,
) -> None:
    """同じ受入れ機関の契約と期間が重ならないこと（両端を含む。終了日が空なら期限なし）。

    DB の排他制約と同じ判定を保存前に行う（admin のインラインでは制約の検査が飛ばされるため）。
    """
    if host_id is None or starts_on is None:
        return
    if ends_on is not None and ends_on < starts_on:
        raise ValidationError(_("The end date must be on or after the start date."), code="period")
    overlapping = SupportContract.objects.filter(host_organization_id=host_id).filter(
        Q(ends_on__isnull=True) | Q(ends_on__gte=starts_on)
    )
    if ends_on is not None:
        overlapping = overlapping.filter(starts_on__lte=ends_on)
    if exclude_pk is not None:
        overlapping = overlapping.exclude(pk=exclude_pk)
    if overlapping.exists():
        raise ValidationError(
            _("This period overlaps another support contract of the same host organization."),
            code="overlap",
        )


def periods_overlap(a: tuple[date, date | None], b: tuple[date, date | None]) -> bool:
    """両端を含む期間の重なり（終了日が None なら期限なし）。"""
    (a_start, a_end), (b_start, b_end) = a, b
    return (a_end is None or a_end >= b_start) and (b_end is None or b_end >= a_start)


def _active_on_q(on: date) -> Q:
    return Q(starts_on__lte=on) & (Q(ends_on__isnull=True) | Q(ends_on__gte=on))


def active_contract(host: HostOrganization, on: date) -> SupportContract | None:
    return host.support_contracts.filter(_active_on_q(on)).first()


def active_contract_count(on: date) -> int:
    """その日に支援委託契約が有効な受入れ機関の数（2027 年要件の「担当機関数」の元）。"""
    return int(
        SupportContract.objects.filter(_active_on_q(on))
        .values("host_organization")
        .distinct()
        .count()
    )
