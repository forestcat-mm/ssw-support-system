"""キャリアアッププラン・実務経験証明書（自機関のテンプレート）のデータを、雇用契約から作る（T36、ADR-0025）。

- 対象は雇用契約（T36-D2）。キャリアアッププランは契約の受入れ機関×分野（事業所の上書き）の、
  基準日に適用される版。使った版の番号と ID をデータ（スナップショット）に残す。
- 実務経験証明書の期間は雇用契約上の期間（開始日〜終了の登録の日・期間の終了日・基準日の早い方）
  で、休業等の期間を含む（T36-D7）。
- キーは tools/forms/make_own_templates.py の欄の名前に「p.」「e.」を付けたもの。
"""

from __future__ import annotations

import datetime
from typing import Any

from apps.contracts.models import EmploymentContract
from services import career_plans, contracts
from services.form_data_contract import representative

LEVEL_ROWS = 5


def _japanese(day: datetime.date) -> str:
    return f"{day.year}年{day.month}月{day.day}日"


def _ymd(prefix: str, day: datetime.date) -> dict[str, Any]:
    return {f"{prefix}_y": day.year, f"{prefix}_m": day.month, f"{prefix}_d": day.day}


def _site_and_categories(contract: EmploymentContract, on: datetime.date) -> dict[str, Any]:
    version = contracts.version_on(contract, on)
    if version is None:
        return {}
    return {
        "site_name": version.site.name,
        "field": version.site.field.name,
        "categories": "、".join(c.name for c in version.categories.order_by("pk")),
    }


def build_career_plan(contract: EmploymentContract, made_on: datetime.date) -> dict[str, Any]:
    host = contract.host_organization
    data: dict[str, Any] = {
        **_ymd("made", made_on),
        "worker_name": contract.worker.name_romaji,
        "host_name": host.name,
        "host_representative": representative(host),
        **_site_and_categories(contract, made_on),
    }
    version = career_plans.plan_version_for(contract, made_on)
    if version is not None:
        data |= {
            "plan_version": version.version,
            "plan_version_id": version.pk,
            "applies_from": version.applies_from,
            "career_route": version.career_route,
            "transfer_scope": version.transfer_scope,
        }
        for row, level in enumerate(version.levels.order_by("order", "pk")[:LEVEL_ROWS], 1):
            requirements = level.requirements
            treatment = "　".join(part for part in (level.position, level.treatment) if part)
            data |= {
                f"l{row}_name": level.name,
                f"l{row}_years": level.years,
                f"l{row}_duties": level.duties,
                f"l{row}_requirements": requirements,
                f"l{row}_treatment": treatment,
            }
    return {f"p.{key}": value for key, value in data.items()}


def period_end(contract: EmploymentContract, on: datetime.date) -> datetime.date:
    ends = [d for d in (contract.terminated_on, contract.current_ends_on, on) if d is not None]
    return min(ends)


def period_start(contract: EmploymentContract) -> datetime.date:
    first = contract.versions.order_by("version").first()
    return first.starts_on if first is not None else contract.current_starts_on


def build_experience_certificate(
    contract: EmploymentContract, issued_on: datetime.date
) -> dict[str, Any]:
    worker = contract.worker
    host = contract.host_organization
    start, end = period_start(contract), period_end(contract, issued_on)
    version = contracts.version_on(contract, end)
    data: dict[str, Any] = {
        **_ymd("issued", issued_on),
        "worker_name": worker.name_romaji,
        "birth_date": worker.birth_date,
        "nationality": worker.nationality.name if worker.nationality_id else "",
        "period": f"{_japanese(start)}から{_japanese(end)}まで",
        "host_name": host.name,
        "host_address": host.address,
        "host_representative": representative(host),
    }
    if version is not None:
        data |= {
            "field": version.site.field.name,
            "categories": "、".join(c.name for c in version.categories.order_by("pk")),
            "site": f"{version.site.name}（{version.site.address}）",
            "duties": version.duties,
        }
    return {f"e.{key}": value for key, value in data.items()}
