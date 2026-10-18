"""見本の生成の対象とデータ（T29・T30。開発者が基盤を確かめるため）。

全欄の元データは T31・T32 で作る。

- 第1-17号: 対象は今の受入れ機関の計画の最新の版（T31 の全欄。form_data_1_17。申告あり）
- 第5-9号・第5-8号: 対象は最新の事前ガイダンス・生活オリエンテーションの記録（無ければ外国人。
  生成前の検査で「記録がありません」になる）。データは form_data_briefing（T32）
"""

from __future__ import annotations

import datetime
from typing import Any

from django.db import models
from django.utils import timezone

from apps.support.models import Briefing, SupportPlan, SupportPlanVersion
from apps.workers.models import ForeignWorker
from services import contracts, form_data_1_17, form_data_briefing
from services.support_catalog import SupportItem

BRIEFING_FORMS = {"5-9": SupportItem.PRE_GUIDANCE, "5-8": SupportItem.ORIENTATION}


def latest_briefing(worker: ForeignWorker, kind: SupportItem) -> Briefing | None:
    return (
        Briefing.objects.filter(
            record__worker=worker, record__kind=kind, record__voided_at__isnull=True
        )
        .order_by("-record__occurred_on", "-pk")
        .first()
    )


def latest_plan_version(worker: ForeignWorker) -> SupportPlanVersion | None:
    """今の受入れ機関の計画（無ければ最新の計画）の、最新の版。"""
    plans = SupportPlan.objects.filter(worker=worker).order_by("-created_at")
    contract = contracts.contract_on(worker, timezone.localdate())
    if contract is not None:
        plans = plans.filter(host_organization=contract.host_organization) or plans
    plan = plans.first()
    return plan.versions.order_by("-version").first() if plan else None


def sample(
    form_id: str, worker: ForeignWorker, basis_on: datetime.date | None = None
) -> tuple[models.Model, dict[str, Any]]:
    """見本の生成の対象とデータ。"""
    if form_id == "1-17":
        version = latest_plan_version(worker)
        if version is None:
            return worker, {}
        return version, form_data_1_17.build(version, basis_on or timezone.localdate()) | (
            form_data_1_17.declaration_values(True)
        )
    if form_id in BRIEFING_FORMS:
        briefing = latest_briefing(worker, BRIEFING_FORMS[form_id])
        if briefing is None:
            return worker, {}
        return briefing, form_data_briefing.build(briefing)
    raise ValueError(f"no sample data for {form_id}")
