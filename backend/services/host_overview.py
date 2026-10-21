"""受入れ機関の詳細の上の帯と今やること（U7）。表示のための集め方だけ。

- 上の帯: 主な分野（有効な事業所の多い分野）・受入れ継続の印・
  支援委託契約の期限（期限のレベルの色）。
- 今やること: この受入れ機関が対象の、担当者の無い開いている
  届出タスク（加入証明の更新・支援委託契約の更新など）。
  外国人に紐づくタスクは外国人の詳細の今やることに出る。
"""

from __future__ import annotations

import datetime
import re
from collections import Counter
from dataclasses import dataclass

from apps.hosts.models import HostOrganization, SpecifiedSkillField, SupportContract
from services import dashboard, deadlines, filings
from services.host_detail import HostDetail
from services.worker_overview import TONES


@dataclass(frozen=True)
class ContractExpiry:
    contract: SupportContract
    deadline: deadlines.Deadline | None  # 終了日の無い・終了を登録した契約は None
    level: deadlines.Level | None
    tone: str


@dataclass(frozen=True)
class HostHeader:
    initial: str
    main_field: SpecifiedSkillField | None
    contract: ContractExpiry | None


# 頭文字に使わない頭の語（括弧の注記と法人の種類）
_PREFIX = re.compile(
    r"^(?:[（(][^）)]*[）)]\s*)*"
    r"(?:株式会社|有限会社|合同会社|合資会社|合名会社|一般社団法人|公益社団法人|社会福祉法人)?\s*"
)


def initial_of(host: HostOrganization) -> str:
    """写真の代わりの丸の頭文字: 括弧の注記と法人の種類を除いた名前の最初の字。"""
    name = _PREFIX.sub("", host.name.strip())
    return (name or host.name.strip() or "?")[0]


def header(detail: HostDetail, today: datetime.date) -> HostHeader:
    active = [site for site in detail.sites if site.is_active]
    counts = Counter(site.field_id for site in active)
    fields = {site.field_id: site.field for site in active}
    main = min(fields.values(), key=lambda f: (-counts[f.pk], f.sort_order)) if fields else None
    contract = detail.current_support_contract
    expiry = None
    if contract is not None:
        row = next((row for row in detail.support_contracts if row.item == contract), None)
        deadline = row.deadline if row else None
        level = deadlines.assess(deadline, today).level if deadline else None
        expiry = ContractExpiry(
            contract=contract,
            deadline=deadline,
            level=level,
            tone=TONES[level] if level else "neutral",
        )
    return HostHeader(initial=initial_of(detail.host), main_field=main, contract=expiry)


def todos(host: HostOrganization, today: datetime.date) -> list[dashboard.FilingRow]:
    """この受入れ機関が対象の、担当者の無い開いている届出タスク（期限の近い順）。"""
    listed = (
        dashboard.base_queryset()
        .filter(worker__isnull=True, assignee__isnull=True, status__in=list(filings.OPEN_STATUSES))
        .select_related("content_type")
        .prefetch_related("target")
        .order_by("unadjusted_due_on", "pk")
    )
    mine = [filing for filing in listed if dashboard.host_id_of(filing.target) == host.pk]
    return dashboard.filing_rows(mine, today)
