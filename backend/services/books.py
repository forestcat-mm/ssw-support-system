"""登録支援機関の帳簿（施行規則第19条の21第5号、運用要領 本体 印刷150〜153。T44、ADR-0036）。

帳簿（支援の実施体制・支援の委託契約・支援対象者・支援の実施の 4 つの管理簿）は、支援の対象の
外国人が締結した特定技能雇用契約の終了の日から 1 年以上、支援業務を行う事務所に備えて置く。
システムは記録を消さないので下限はいつも満たす。ここでは保存義務の満了日の目安だけを出す
（削除・匿名化の手順は本番準備か段階5。T44-D5）。
"""

from __future__ import annotations

import datetime

from apps.contracts.models import EmploymentContract
from apps.workers.models import ForeignWorker
from services import deadlines

RETENTION_YEARS = 1  # 雇用契約の終了の日から 1 年以上（運用要領 本体 印刷153、別冊 32頁）


def contract_end(contract: EmploymentContract) -> datetime.date | None:
    """雇用契約の終了の日（終了を登録していればその日、無ければ今の版の満了日。無期なら None）。"""
    return contract.terminated_on or contract.current_ends_on


def retention_until(worker: ForeignWorker) -> datetime.date | None:
    """帳簿の保存義務の満了日の目安（外国人の最後の雇用契約の終了の日＋1 年）。

    終了の日が決まっていない契約（無期・登録の途中）があれば None（まだ決まらない）。
    契約が無ければ None。
    """
    ends = [contract_end(contract) for contract in EmploymentContract.objects.filter(worker=worker)]
    if not ends or any(end is None for end in ends):
        return None
    last = max(end for end in ends if end is not None)
    return deadlines.add_months(last, 12 * RETENTION_YEARS)
