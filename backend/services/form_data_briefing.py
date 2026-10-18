"""事前ガイダンス・生活オリエンテーションの確認書（第5-9号・第5-8号）のデータを、記録から作る（T32、ADR-0021）。

確認書の欄は、実施日時の 3 行・所属機関（又は登録支援機関）の氏名又は名称・説明者の氏名だけ
（細目・該当なし・費用の欄は無い。T32-D1）。署名と署名日は手で書く。
- 実施日時: 欄の空白を値で置き換える（T32-D2）。数字は全角。年は 4 字、ほかは 2 字にそろえ
  （1 桁は前に全角空白）、分は 2 桁（「００」）。行が無い欄は元の空白（年 4 字・ほか 2 字）。
  4 回以上の実施は最初の 3 回を書く（T32-D3。生成前の警告 too_many_rows）。
- 所属機関名（第5-8号の記載例 ★3）: 登録支援機関に支援の全部を委託した場合だけ登録支援機関
  （自機関）の名称、そうでなければ特定技能所属機関の名称。第5-9号も同じ欄なので同じにする（ADR-0021）。
- 説明者の氏名（★3「実施した者の氏名」）: 記録の担当者（T32-D4）。
"""

from __future__ import annotations

from typing import Any

from apps.core.models import Organization
from apps.hosts.models import SupportScope
from apps.support.models import Briefing, BriefingSession
from services import contracts, hosts

SP = "　"
FULL_WIDTH = str.maketrans("0123456789", "０１２３４５６７８９")
FORM_ROWS = 3
SLOT_WIDTHS = {
    "year": 4,
    "month": 2,
    "day": 2,
    "start_hour": 2,
    "start_minute": 2,
    "end_hour": 2,
    "end_minute": 2,
}


def digits(value: int, width: int, *, zero_pad: bool = False) -> str:
    """全角の数字（width 字にそろえる。zero_pad なら「０」、ほかは全角空白で前を埋める）。"""
    text = str(value).zfill(width) if zero_pad else str(value)
    return text.translate(FULL_WIDTH).rjust(width, SP)


def session_values(row: int, session: BriefingSession | None) -> dict[str, str]:
    prefix = f"session{row}."
    if session is None:
        return {prefix + name: SP * width for name, width in SLOT_WIDTHS.items()}
    return {
        prefix + "year": digits(session.occurred_on.year, 4),
        prefix + "month": digits(session.occurred_on.month, 2),
        prefix + "day": digits(session.occurred_on.day, 2),
        prefix + "start_hour": digits(session.start_time.hour, 2),
        prefix + "start_minute": digits(session.start_time.minute, 2, zero_pad=True),
        prefix + "end_hour": digits(session.end_time.hour, 2),
        prefix + "end_minute": digits(session.end_time.minute, 2, zero_pad=True),
    }


def organization_name(briefing: Briefing) -> str:
    """所属機関（又は登録支援機関）の氏名又は名称（★3。全部委託なら自機関、そうでなければ受入れ機関）。"""
    on = briefing.record.occurred_on
    contract = contracts.contract_on(briefing.worker, on)
    host = contract.host_organization if contract is not None else None
    organization = Organization.objects.first()
    if host is not None:
        support_contract = hosts.active_contract(host, on)
        if support_contract is None or support_contract.scope != SupportScope.FULL:
            return host.name
    return organization.name if organization is not None else ""


def build(briefing: Briefing) -> dict[str, Any]:
    sessions = list(
        BriefingSession.objects.filter(briefing=briefing).order_by(
            "occurred_on", "start_time", "pk"
        )[:FORM_ROWS]
    )
    data: dict[str, Any] = {
        "briefing.organization_name": organization_name(briefing),
        "briefing.explainer_name": briefing.record.staff.name if briefing.record.staff else "",
    }
    for row in range(1, FORM_ROWS + 1):
        data |= session_values(row, sessions[row - 1] if row <= len(sessions) else None)
    return data
