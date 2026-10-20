"""受入れ機関・分野の書類（第3-1号・第13-1号・第13-2号・第5-10号・第1-25号）のデータ（T37、ADR-0026）。

対象は雇用契約（外国人×受入れ機関）。支援委託契約は基準日に有効なもの（受入れ機関ごと。ADR-0015）で、
第5-10号・第1-25号は対象の外国人ごとに作る（T37-D6）。作成責任者は生成の画面で入れる（初期値は
受入れ機関の代表者。第13-2号は自機関の代表者。T37-D10）。キーは
tools/forms/make_application_templates.py の欄の名前に「a.」を付けたもの。
"""

from __future__ import annotations

import datetime
import re
from typing import Any

from apps.contracts.models import EmploymentContract
from apps.core.models import Organization
from apps.hosts.models import FeeTiming, SupportContract, SupportScope
from apps.workers.models import Sex
from services import contracts, hosts
from services.form_data_contract import representative

SP = "　"
SEX_WORDS: dict[str, str] = {Sex.MALE: "男", Sex.FEMALE: "女"}
REGISTRATION = re.compile(r"^\s*(\d+)\s*登\s*[-－ー―]\s*(\d+)\s*$")
FEE_ROWS = 10


def japanese(day: datetime.date) -> str:
    return f"{day.year}年{day.month}月{day.day}日"


def _ymd(prefix: str, day: datetime.date | None) -> dict[str, Any]:
    if day is None:
        return {}
    return {f"{prefix}_y": day.year, f"{prefix}_m": day.month, f"{prefix}_d": day.day}


def organization_representative(organization: Organization | None) -> str:
    if organization is None:
        return ""
    return SP.join(
        part
        for part in (organization.representative_title, organization.representative_name)
        if part
    )


def default_author(form_id: str, contract: EmploymentContract) -> str:
    """作成責任者の初期値（第13-2号は自機関、ほかは受入れ機関の代表者の役職・氏名）。"""
    if form_id == "13-2":
        return organization_representative(Organization.objects.first())
    return representative(contract.host_organization)


def support_contract_on(contract: EmploymentContract, on: datetime.date) -> SupportContract | None:
    return hosts.active_contract(contract.host_organization, on)


def field_name(contract: EmploymentContract, on: datetime.date) -> str:
    version = contracts.version_on(contract, on)
    if version is None:
        return ""
    name = version.site.field.name
    return name[: -len("分野")] if name.endswith("分野") else name


def _person(contract: EmploymentContract, prefix_space: bool) -> dict[str, Any]:
    worker = contract.worker
    host = contract.host_organization
    pad = SP if prefix_space else ""
    return {
        "host_name": pad + host.name,
        "host_address": pad + host.address,
        "worker_name": pad + worker.name_romaji,
        "worker_sex": pad + SEX_WORDS.get(worker.sex, ""),
        "worker_nationality": pad + (worker.nationality.name if worker.nationality_id else ""),
        "worker_birth_date": pad + japanese(worker.birth_date),
    }


def build(
    form_id: str, contract: EmploymentContract, on: datetime.date, *, author: str
) -> dict[str, Any]:
    organization = Organization.objects.first()
    data: dict[str, Any] = {}
    if form_id in ("3-1", "13-1", "13-2"):
        data |= _person(contract, prefix_space=True) | _ymd("made", on) | {"author": author}
        if form_id == "13-2" and organization is not None:
            data |= {
                "organization_name": SP + organization.name,
                "organization_address": SP + organization.address,
            }
    elif form_id == "5-10":
        data |= _support_contract_5_10(contract, on, organization)
    elif form_id == "1-25":
        data |= _explanation_1_25(contract, on, organization, author)
    return {f"a.{key}": value for key, value in data.items()}


def _support_contract_5_10(
    contract: EmploymentContract, on: datetime.date, organization: Organization | None
) -> dict[str, Any]:
    host = contract.host_organization
    support = support_contract_on(contract, on)
    data: dict[str, Any] = {
        "host_name": host.name,
        "organization_name": organization.name if organization else "",
        "worker_name": contract.worker.name_romaji,
        "field": field_name(contract, on),
        "court": organization.court_name if organization else "",
        "host_signature": SP.join(p for p in (host.name, representative(host)) if p),
        "organization_signature": SP.join(
            p
            for p in (
                organization.name if organization else "",
                organization_representative(organization),
            )
            if p
        ),
    }
    if support is None:
        return data
    data |= {
        "monthly_fee": support.monthly_fee,
        **_ymd("starts", support.starts_on),
        **_ymd("ends", support.ends_on),
        **_ymd("concluded", support.concluded_on),
    }
    fees = list(support.fees.all())
    for row, fee in enumerate(fees[:FEE_ROWS], start=1):
        data |= {
            f"fee{row}_name": fee.name,
            f"fee{row}_amount": fee.amount,
            f"fee{row}_occasional": fee.timing == FeeTiming.OCCASIONAL,
            f"fee{row}_regular": fee.timing == FeeTiming.REGULAR,
        }
    if fees:
        data["fee_total"] = sum(fee.amount for fee in fees)
    return data


def _explanation_1_25(
    contract: EmploymentContract,
    on: datetime.date,
    organization: Organization | None,
    author: str,
) -> dict[str, Any]:
    support = support_contract_on(contract, on)
    data: dict[str, Any] = {
        "worker_name": contract.worker.name_romaji,
        "organization_name": organization.name if organization else "",
        "host_name": SP + contract.host_organization.name,
        **_ymd("made", on),
        "author": SP + author if author else "",
    }
    if organization is not None:
        found = REGISTRATION.match(organization.registration_number)
        if found:
            data |= {"registration_year": found.group(1), "registration_serial": found.group(2)}
        else:
            data["registration_serial"] = organization.registration_number
    if support is not None:
        data |= {
            **_ymd("concluded", support.concluded_on),
            "full_support": "該当" if support.scope == SupportScope.FULL else "非該当",
            "monthly_fee_text": f"{support.monthly_fee:,}円"
            if support.monthly_fee is not None
            else "",
            **_ymd("starts", support.starts_on),
            **_ymd("ends", support.ends_on),
        }
    return data


def build_1_26(
    made_on: datetime.date, *, relief_on: datetime.date | None, other_reason: str
) -> dict[str, Any]:
    """第1-26号 公的義務履行に関する誓約書（申請人が書く。T42 の追加）。

    理由は、納付緩和措置・免除措置を申請した日（relief_on）か、やむを得ない理由の文。署名は手で書く。
    """
    data: dict[str, Any] = _ymd("made", made_on)
    if relief_on is not None:
        data |= {"relief": True, "other": False} | _ymd("relief", relief_on)
    elif other_reason.strip():
        data |= {"relief": False, "other": True, "other_reason": other_reason.strip()}
    return {f"a.{key}": value for key, value in data.items()}
