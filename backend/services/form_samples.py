"""見本の生成の対象とデータ（T29・T30。開発者が基盤を確かめるため）。

全欄の元データは T31・T32 で作る。

- 第1-17号: 対象は今の受入れ機関の計画の最新の版（T31 の全欄。form_data_1_17。申告あり）
- 第5-9号: 対象は最新の事前ガイダンスの記録（無ければ外国人。生成前の検査で「記録がありません」に
  なる）。所属機関（自機関）の名称・説明者（担当者）・最初の実施日時
"""

from __future__ import annotations

import datetime
from typing import Any

from django.db import models
from django.utils import timezone

from apps.support.models import Briefing, BriefingSession, SupportPlan, SupportPlanVersion
from apps.workers.models import ForeignWorker
from services import contracts, form_data_1_17, organization
from services.support_catalog import SupportItem


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
    if form_id == "5-9":
        data: dict[str, Any] = {
            "organization.name": organization.organization_display_name(),
            "staff.name": worker.assigned_staff.name if worker.assigned_staff else "",
        }
        briefing = latest_briefing(worker, SupportItem.PRE_GUIDANCE)
        if briefing is None:
            return worker, data
        session = (
            BriefingSession.objects.filter(briefing=briefing)
            .order_by("occurred_on", "start_time")
            .first()
        )
        if session is not None:
            data |= {
                "session1.date": session.occurred_on,
                "session1.start_hour": session.start_time.hour,
                "session1.start_minute": session.start_time.minute,
                "session1.end_hour": session.end_time.hour,
                "session1.end_minute": session.end_time.minute,
            }
        return briefing, data
    raise ValueError(f"no sample data for {form_id}")
