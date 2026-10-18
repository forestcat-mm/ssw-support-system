"""見本の生成の対象とデータ（T29・T30。開発者が基盤を確かめるため）。

全欄の元データは T31・T32 で作る。

- 第1-17号: 対象は外国人。Ⅰ 支援対象者の氏名・生年月日・国籍（ほかの欄は空）
- 第5-9号: 対象は最新の事前ガイダンスの記録（無ければ外国人。生成前の検査で「記録がありません」に
  なる）。所属機関（自機関）の名称・説明者（担当者）・最初の実施日時
"""

from __future__ import annotations

from typing import Any

from django.db import models

from apps.support.models import Briefing, BriefingSession
from apps.workers.models import ForeignWorker
from services import organization
from services.support_catalog import SupportItem


def latest_briefing(worker: ForeignWorker, kind: SupportItem) -> Briefing | None:
    return (
        Briefing.objects.filter(
            record__worker=worker, record__kind=kind, record__voided_at__isnull=True
        )
        .order_by("-record__occurred_on", "-pk")
        .first()
    )


def sample(form_id: str, worker: ForeignWorker) -> tuple[models.Model, dict[str, Any]]:
    """見本の生成の対象とデータ。"""
    if form_id == "1-17":
        return worker, {
            "worker.name_romaji": worker.name_romaji,
            "worker.birth_date": worker.birth_date,
            "worker.nationality": worker.nationality.name,
        }
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
