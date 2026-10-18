"""支援委託契約の締結・変更・終了の届出タスク（第5章 F07、T28c、ADR-0017）。

所属機関が外国人ごとに届け出る（入管法施行規則 第19条の17。14 日以内）。対象の外国人は
services.hosts.support_targets（事由の日に雇用契約が有効な特定技能1号の人）。

- 締結（新しい締結）: 第3-3-2号（B 契約の締結）と第3-2号（支援計画変更。別表Ⅲ⑥）。起算日は締結日。
- 続く行（前の行に終了の登録が無く、前の行の予定の終了日の翌日に始まる行）: 締結として起票しない。
  委託料が前の行と違うときだけ第3-3-1号（委託料の変更）。起算日は続く行の開始日。
- 変更（同じ行の委託料・期間を「契約の変更」として直したとき）: 第3-3-1号。起算日は変更日。
  誤りの訂正として直したときは起票しない。
- 終了の登録: 第3-3-2号（A 契約の終了）と第3-2号（別表Ⅲ⑦）。起算日は実際の終了日。
- 終了の取消: 起票したタスクを共通の規則（services.filings.revert_raised_task）で戻す。未着手は
  理由つきで取消、対応中は印を付けて残し（画面で知らせる）、提出済み・完了などはメモを付けて残す。
- 外国人の退職で終わる場合は第3-1-2号の c 欄で兼ねる（services.filings.on_contract_terminated）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from django.utils.translation import gettext as _

from apps.core.models import User
from apps.filings.models import Filing, FilingKind, FilingStatus, Trigger
from apps.hosts.models import SupportContract
from apps.workers.models import ForeignWorker
from services import filings, hosts


@dataclass
class Raised:
    """起票の結果（画面で知らせる）。"""

    created: list[Filing] = field(default_factory=list)
    workers: list[ForeignWorker] = field(default_factory=list)
    left_open: list[Filing] = field(default_factory=list)  # 取消のときに動かさなかったもの


def _raise(
    kind: FilingKind,
    worker: ForeignWorker,
    *,
    basis_on: date,
    trigger: Trigger,
    details: dict[str, Any],
    by: User | None,
    out: Raised,
) -> None:
    filing, created = filings.create_if_active(
        kind, target=worker, basis_on=basis_on, trigger=trigger, by=by, details=details
    )
    if filing is not None and created:
        out.created.append(filing)


def _raise_end_or_conclusion(
    contract: SupportContract,
    *,
    event: str,
    on: date,
    trigger: Trigger,
    details: dict[str, Any],
    by: User | None,
) -> Raised:
    out = Raised(workers=hosts.support_targets(contract.host_organization, on))
    base = {"support_contract": contract.pk, "event": event} | details
    for worker in out.workers:
        _raise(
            FilingKind.SUPPORT_CONTRACT_END_OR_CONCLUSION,
            worker,
            basis_on=on,
            trigger=trigger,
            details=base,
            by=by,
            out=out,
        )
        # 支援計画も変わる（運用要領 本体 別表Ⅲ⑥⑦、印刷 110頁。第3-3-2号より前に提出）
        _raise(
            FilingKind.SUPPORT_PLAN_CHANGE,
            worker,
            basis_on=on,
            trigger=trigger,
            details=base | {"plan_change_item": "III-6" if event == "concluded" else "III-7"},
            by=by,
            out=out,
        )
    return out


def on_registered(contract: SupportContract, *, by: User | None) -> Raised:
    """支援委託契約を登録したとき（新しい締結か、続く行か）。"""
    previous = hosts.previous_row(contract)
    if previous is not None:
        # 続く行: 締結としては起票しない。委託料が変われば第3-3-1号
        if (
            previous.monthly_fee is not None
            and contract.monthly_fee is not None
            and previous.monthly_fee != contract.monthly_fee
        ):
            return on_amended(
                contract,
                changes={"monthly_fee": [previous.monthly_fee, contract.monthly_fee]},
                on=contract.starts_on,
                by=by,
                previous=previous,
            )
        return Raised()
    return _raise_end_or_conclusion(
        contract,
        event="concluded",
        on=contract.concluded_on,
        trigger=Trigger.SUPPORT_CONTRACT_CONCLUDED,
        details={"concluded_on": contract.concluded_on.isoformat()},
        by=by,
    )


def on_amended(
    contract: SupportContract,
    *,
    changes: dict[str, list[Any]],
    on: date,
    by: User | None,
    previous: SupportContract | None = None,
) -> Raised:
    """委託料・契約期間の変更（第3-3-1号）。changes は {項目: [変更前, 変更後]}。"""
    out = Raised(workers=hosts.support_targets(contract.host_organization, on))
    details: dict[str, Any] = {
        "support_contract": contract.pk,
        "event": "changed",
        "changed_on": on.isoformat(),
        "changes": {key: [_json(a), _json(b)] for key, (a, b) in changes.items()},
    }
    if previous is not None:
        details["previous_support_contract"] = previous.pk
    for worker in out.workers:
        _raise(
            FilingKind.SUPPORT_CONTRACT_CHANGE,
            worker,
            basis_on=on,
            trigger=Trigger.SUPPORT_CONTRACT_CHANGED,
            details=details,
            by=by,
            out=out,
        )
    return out


def on_terminated(contract: SupportContract, *, by: User | None) -> Raised:
    """終了の登録（第3-3-2号 A と第3-2号）。"""
    assert contract.terminated_on is not None
    return _raise_end_or_conclusion(
        contract,
        event="terminated",
        on=contract.terminated_on,
        trigger=Trigger.SUPPORT_CONTRACT_TERMINATED,
        details={
            "terminated_on": contract.terminated_on.isoformat(),
            "termination_category": contract.termination_category,
            "termination_reason": contract.termination_reason,
            "termination_note": contract.termination_note,
        },
        by=by,
    )


def on_termination_cancelled(
    contract: SupportContract, *, terminated_on: date, reason: str, by: User | None
) -> Raised:
    """終了の取消: 起票したタスクを共通の規則で戻す（対応中で残したものを left_open に返す）。"""
    out = Raised()
    note = _("The termination of the support contract was cancelled: %(reason)s") % {
        "reason": reason
    }
    tasks = Filing.objects.filter(
        kind__in=[FilingKind.SUPPORT_CONTRACT_END_OR_CONCLUSION, FilingKind.SUPPORT_PLAN_CHANGE],
        trigger=Trigger.SUPPORT_CONTRACT_TERMINATED,
        basis_on=terminated_on,
        trigger_details__support_contract=contract.pk,
    ).exclude(status=FilingStatus.CANCELLED)
    for filing in tasks.order_by("pk"):
        outcome = filings.revert_raised_task(
            filing, source="support_contract_termination", reason=note, by=by
        )
        if outcome is filings.Reverted.LEFT_OPEN:
            out.left_open.append(filing)
    return out


def _json(value: Any) -> Any:
    return value.isoformat() if isinstance(value, date) else value
