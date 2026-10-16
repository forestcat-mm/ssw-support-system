"""業務区分（分野→区分の 2 階層）。適用開始日・終了日で、ある日に有効な区分を引く。"""

from __future__ import annotations

from datetime import date

from django.db.models import Q, QuerySet

from apps.hosts.models import OperationCategory, SpecifiedSkillField


def effective_on_q(on: date) -> Q:
    """開始日が空または on 以前、かつ終了日が空または on 以後。"""
    return (Q(valid_from__isnull=True) | Q(valid_from__lte=on)) & (
        Q(valid_until__isnull=True) | Q(valid_until__gte=on)
    )


def effective_categories(field: SpecifiedSkillField, on: date) -> QuerySet[OperationCategory]:
    return field.categories.filter(effective_on_q(on)).order_by("sort_order", "pk")
