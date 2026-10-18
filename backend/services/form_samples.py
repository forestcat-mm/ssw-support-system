"""見本の生成のデータ（T29。開発者が基盤を確かめるため。全欄の元データは T31・T32 で作る）。

- 第1-17号: Ⅰ 支援対象者の氏名・生年月日・国籍（ほかの欄は空）
- 第5-9号: 所属機関（自機関）の名称・説明者（担当者）・最新の事前ガイダンスの最初の実施日時
"""

from __future__ import annotations

from typing import Any

from apps.support.models import Briefing, BriefingSession
from apps.workers.models import ForeignWorker
from services import organization
from services.support_catalog import SupportItem


def sample_data(form_id: str, worker: ForeignWorker) -> dict[str, Any]:
    if form_id == "1-17":
        return {
            "worker.name_romaji": worker.name_romaji,
            "worker.birth_date": worker.birth_date,
            "worker.nationality": worker.nationality.name,
        }
    if form_id == "5-9":
        data: dict[str, Any] = {
            "organization.name": organization.organization_display_name(),
            "staff.name": worker.assigned_staff.name if worker.assigned_staff else "",
        }
        briefing = (
            Briefing.objects.filter(
                record__worker=worker,
                record__kind=SupportItem.PRE_GUIDANCE,
                record__voided_at__isnull=True,
            )
            .order_by("-record__occurred_on", "-pk")
            .first()
        )
        session = (
            BriefingSession.objects.filter(briefing=briefing)
            .order_by("occurred_on", "start_time")
            .first()
            if briefing
            else None
        )
        if session is not None:
            data |= {
                "session1.date": session.occurred_on,
                "session1.start_hour": session.start_time.hour,
                "session1.start_minute": session.start_time.minute,
                "session1.end_hour": session.end_time.hour,
                "session1.end_minute": session.end_time.minute,
            }
        return data
    raise ValueError(f"no sample data for {form_id}")
