"""監査ログの検索（設定画面、T15b）。読み取り専用。

検索・行の表示は、それ自体を「監査ログの閲覧」として 1 回につき 1 件残す
（context は条件と件数だけ）。
既定では「監査ログの閲覧」の行を除く（自分の閲覧で検索結果が埋まらないように）。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

from django.db.models import Q, QuerySet
from django.utils import timezone

from apps.core.models import AuditAction, AuditLog
from services import audit


@dataclass(frozen=True)
class AuditQuery:
    actor_id: int | None = None
    action: str = ""
    content_type_id: int | None = None
    target: str = ""  # 対象の名称（部分一致）または ID（完全一致）
    date_from: datetime.date | None = None
    date_to: datetime.date | None = None
    include_audit_views: bool = False


def _start_of(day: datetime.date) -> datetime.datetime:
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time.min))


def search(query: AuditQuery) -> QuerySet[AuditLog]:
    logs = AuditLog.objects.select_related("content_type", "actor")
    if not query.include_audit_views and query.action != AuditAction.AUDIT_VIEW:
        logs = logs.exclude(action=AuditAction.AUDIT_VIEW)
    if query.actor_id is not None:
        logs = logs.filter(actor_id=query.actor_id)
    if query.action:
        logs = logs.filter(action=query.action)
    if query.content_type_id is not None:
        logs = logs.filter(content_type_id=query.content_type_id)
    if query.target.strip():
        text = query.target.strip()
        logs = logs.filter(Q(object_repr__icontains=text) | Q(object_id=text))
    if query.date_from is not None:
        logs = logs.filter(occurred_at__gte=_start_of(query.date_from))
    if query.date_to is not None:
        logs = logs.filter(occurred_at__lt=_start_of(query.date_to + datetime.timedelta(days=1)))
    return logs.order_by("-occurred_at", "-pk")


def record_audit_view(conditions: dict[str, Any], count: int) -> AuditLog:
    """監査ログの検索・行の表示を 1 件残す（条件と件数だけ。結果の中身は残さない）。"""
    safe = {key: str(value) for key, value in conditions.items() if value not in (None, "", False)}
    return audit.record(AuditAction.AUDIT_VIEW, context={"conditions": safe, "count": count})
