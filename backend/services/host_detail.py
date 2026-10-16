"""受入れ機関の一覧・詳細の表示用の組み立て（第5章 F02、T13e）。

- 期限: 加入状況は有効期限（document_expiry）、協力確認書は提出先ごとの最新の添付の有効期限
  （document_expiry）、支援委託契約は終了日（support_contract_end）。
- 所属する外国人: 外国人一覧と同じ判定（現在の雇用契約が有効または予定）。無効化済みは除く。
"""

from __future__ import annotations

import datetime
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from django.contrib.contenttypes.models import ContentType

from apps.core.models import Attachment, User
from apps.hosts.models import (
    HostCooperationConfirmation,
    HostMembership,
    HostOrganization,
    HostSite,
    SpecifiedSkillField,
    SupportContract,
)
from services import authorization, deadlines, worker_list
from services.worker_list import WorkerListQuery, WorkerRow


def _earliest(items: Iterable[deadlines.Deadline | None]) -> deadlines.Deadline | None:
    present = [item for item in items if item is not None]
    return min(present, key=lambda item: item.unadjusted_due_on) if present else None


def _latest_attachments(model: type[Any], object_ids: Iterable[int]) -> dict[int, list[Attachment]]:
    """対象ごとの添付（新しい順）。"""
    content_type = ContentType.objects.get_for_model(model)
    grouped: dict[int, list[Attachment]] = {}
    for attachment in Attachment.objects.filter(
        content_type=content_type, object_id__in=list(object_ids)
    ).order_by("-created_at", "-pk"):
        grouped.setdefault(attachment.object_id, []).append(attachment)
    return grouped


def _attachment_deadline(attachments: list[Attachment]) -> deadlines.Deadline | None:
    """最新の添付の有効期限（差し替え前の古い証明書は見ない）。"""
    latest = attachments[0] if attachments else None
    if latest is None or latest.valid_until is None:
        return None
    return deadlines.document_expiry(latest.valid_until)


def support_contract_for(
    contracts: list[SupportContract], today: datetime.date
) -> SupportContract | None:
    """表示する支援委託契約: 今日有効 → 開始前（近い順）→ 最後に終わった契約。"""
    active = [
        c for c in contracts if c.starts_on <= today and (c.ends_on is None or c.ends_on >= today)
    ]
    if active:
        return max(active, key=lambda c: c.starts_on)
    upcoming = [c for c in contracts if c.starts_on > today]
    if upcoming:
        return min(upcoming, key=lambda c: c.starts_on)
    return max(contracts, key=lambda c: c.starts_on) if contracts else None


def _contract_deadline(contract: SupportContract | None) -> deadlines.Deadline | None:
    if contract is None or contract.ends_on is None:
        return None
    return deadlines.support_contract_end(contract.ends_on)


# --- 一覧 -----------------------------------------------------------------------


@dataclass(frozen=True)
class HostRow:
    host: HostOrganization
    fields: list[SpecifiedSkillField]
    site_count: int
    worker_count: int
    membership_deadline: deadlines.Deadline | None
    cooperation_deadline: deadlines.Deadline | None
    support_contract: SupportContract | None
    support_contract_deadline: deadlines.Deadline | None


def worker_counts(user: User, today: datetime.date) -> Counter[int]:
    """受入れ機関ごとの所属する外国人の人数（外国人一覧と同じ判定。無効化済みは除く）。"""
    rows = worker_list.list_workers(user, WorkerListQuery(statuses=()), today).rows
    return Counter(row.host_organization_id for row in rows if row.host_organization_id)


def host_rows(user: User, today: datetime.date) -> list[HostRow]:
    host_list = list(
        authorization.scope(user, HostOrganization.objects.all())
        .prefetch_related("sites__field", "memberships", "cooperation_confirmations")
        .prefetch_related("support_contracts")
        .order_by("name_kana", "name")
    )
    counts = worker_counts(user, today)
    cooperation_ids = [
        confirmation.pk
        for host in host_list
        for confirmation in host.cooperation_confirmations.all()
    ]
    cooperation_attachments = _latest_attachments(HostCooperationConfirmation, cooperation_ids)
    rows = []
    for host in host_list:
        sites = [site for site in host.sites.all() if site.is_active]
        fields_by_pk = {site.field.pk: site.field for site in sites}
        contract = support_contract_for(list(host.support_contracts.all()), today)
        rows.append(
            HostRow(
                host=host,
                fields=sorted(fields_by_pk.values(), key=lambda f: f.sort_order),
                site_count=len(sites),
                worker_count=counts[host.pk],
                membership_deadline=_earliest(
                    deadlines.document_expiry(m.valid_until) if m.valid_until else None
                    for m in host.memberships.all()
                ),
                cooperation_deadline=_earliest(
                    _attachment_deadline(cooperation_attachments.get(c.pk, []))
                    for c in host.cooperation_confirmations.all()
                ),
                support_contract=contract,
                support_contract_deadline=_contract_deadline(contract),
            )
        )
    return rows


# --- 詳細 -----------------------------------------------------------------------


@dataclass(frozen=True)
class DatedItem:
    """期限つきの行（加入状況・協力確認書・支援委託契約）と、その添付。"""

    item: Any
    deadline: deadlines.Deadline | None
    attachments: list[Attachment] = field(default_factory=list)


@dataclass(frozen=True)
class HostDetail:
    host: HostOrganization
    sites: list[HostSite]
    memberships: list[DatedItem]
    cooperation_confirmations: list[DatedItem]
    support_contracts: list[DatedItem]
    current_support_contract: SupportContract | None
    workers: list[WorkerRow]


def host_detail(
    host: HostOrganization, user: User, today: datetime.date, *, with_workers: bool
) -> HostDetail:
    sites = list(
        host.sites.select_related("field")
        .prefetch_related("categories")
        .order_by("-is_active", "pk")
    )
    memberships = list(host.memberships.select_related("field").order_by("field__sort_order"))
    membership_files = _latest_attachments(HostMembership, [m.pk for m in memberships])
    confirmations = list(host.cooperation_confirmations.order_by("municipality", "-submitted_on"))
    confirmation_files = _latest_attachments(
        HostCooperationConfirmation, [c.pk for c in confirmations]
    )
    contracts = list(host.support_contracts.order_by("-starts_on"))
    contract_files = _latest_attachments(SupportContract, [c.pk for c in contracts])
    current = support_contract_for(contracts, today)
    workers = (
        worker_list.list_workers(
            user, WorkerListQuery(statuses=(), host_organization_id=host.pk), today
        ).rows
        if with_workers
        else []
    )
    return HostDetail(
        host=host,
        sites=sites,
        memberships=[
            DatedItem(
                m,
                deadlines.document_expiry(m.valid_until) if m.valid_until else None,
                membership_files.get(m.pk, []),
            )
            for m in memberships
        ],
        cooperation_confirmations=[
            DatedItem(
                c,
                _attachment_deadline(confirmation_files.get(c.pk, [])),
                confirmation_files.get(c.pk, []),
            )
            for c in confirmations
        ],
        support_contracts=[
            DatedItem(
                c,
                _contract_deadline(c) if c == current else None,
                contract_files.get(c.pk, []),
            )
            for c in contracts
        ],
        current_support_contract=current,
        workers=workers,
    )
