"""受入れ機関・分野の書類（第3-1号・第13-1号・第13-2号・第5-10号・第1-25号・第1-11-1号・第1-16号・
第1-23号・第1-29号）のデータ（T37、ADR-0026。第1-11-1号は T46b、ADR-0038。第1-16号・1-23号・1-29号は
T54、ADR-0043）。

対象は雇用契約（外国人×受入れ機関）。支援委託契約は基準日に有効なもの（受入れ機関ごと。ADR-0015）で、
第5-10号・第1-25号は対象の外国人ごとに作る（T37-D6）。作成責任者は生成の画面で入れる（初期値は
受入れ機関の代表者。第13-2号は自機関の代表者。T37-D10）。キーは
tools/forms/make_application_templates.py の欄の名前に「a.」を付けたもの。
"""

from __future__ import annotations

import datetime
import json
import re
from typing import Any

from apps.contracts.models import EmploymentContract
from apps.core.models import Organization
from apps.forms_engine.models import GeneratedDocument
from apps.hosts.models import (
    FeeTiming,
    HostOfficer,
    HostOrganization,
    SupportContract,
    SupportScope,
)
from apps.support.models import Briefing
from apps.workers.models import Sex
from services import contracts, hosts
from services.form_data_contract import representative
from services.support_catalog import SupportItem

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
    elif form_id == "1-11-1":
        data |= overview_1_11_1(contract.host_organization, on, author)
    elif form_id == "1-29":
        data |= {"host_name": contract.host_organization.name} | _ymd("made", on)
    elif form_id == "1-23":
        data |= pledge_1_23(contract.host_organization, on, author)
    return {f"a.{key}": value for key, value in data.items()}


# --- 第1-23号・第1-16号（T54、ADR-0043）-------------------------------------------------

PLEDGE_OFFICERS = 8  # 第1-23号の表の行数


def uninvolved_officers(host: HostOrganization, on: datetime.date) -> list[HostOfficer]:
    """基準日に在任の役員のうち、受入れ業務の執行に直接関与しない役員（T54-D2）。"""
    return [
        officer for officer in overview_officers(host, on) if officer.not_involved_in_acceptance
    ]


def pledge_1_23(host: HostOrganization, on: datetime.date, author: str) -> dict[str, Any]:
    data: dict[str, Any] = {"host_name": host.name, "author": author} | _ymd("made", on)
    for number, officer in enumerate(uninvolved_officers(host, on)[:PLEDGE_OFFICERS], start=1):
        data |= {f"officer{number}_kana": officer.name_kana, f"officer{number}_name": officer.name}
    return data


HIRING_FORM_ID = "1-16"
PAID_ROWS = 5
YES, NO = "yes", "no"


def hiring_defaults(contract: EmploymentContract, on: datetime.date) -> dict[str, Any]:
    """第1-16号の入力の初期値。前の生成の入力、無ければ事前ガイダンスの記録から（T54-D3）。

    3 事前ガイダンスの実施は記録があれば「有」。4 自国等の機関に支払った費用は、事前ガイダンスで
    確かめた外国の機関の費用（円の額）。
    """
    previous = (
        GeneratedDocument.objects.filter(template__form_id=HIRING_FORM_ID, worker=contract.worker)
        .order_by("-created_at", "-pk")
        .first()
    )
    if previous is not None:
        inputs = json.loads(previous.snapshot).get("data", {}).get("a.inputs")
        if isinstance(inputs, dict):
            return inputs
    briefing = (
        Briefing.objects.filter(
            worker=contract.worker,
            record__kind=SupportItem.PRE_GUIDANCE,
            record__voided_at__isnull=True,
        )
        .order_by("-record__occurred_on", "-pk")
        .first()
    )
    values: dict[str, Any] = {"guidance": YES if briefing is not None else ""}
    if briefing is not None:
        for number, fee in enumerate(briefing.fees.all()[:PAID_ROWS], start=1):
            values |= {
                f"paid{number}_name": fee.organization_name,
                f"paid{number}_purpose": fee.breakdown[:40],
                f"paid{number}_date": fee.paid_on.isoformat(),
                f"paid{number}_yen": fee.amount_jpy,
            }
    return values


def _day(value: Any) -> datetime.date | None:
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str) and value:
        return datetime.date.fromisoformat(value)
    return None


def build_1_16(
    contract: EmploymentContract, on: datetime.date, *, author: str, inputs: dict[str, Any]
) -> dict[str, Any]:
    """第1-16号 雇用の経緯に係る説明書の値（1・2・4 は入力、3 は入力〔初期値は記録〕）。"""
    data: dict[str, Any] = {
        "worker_name": contract.worker.name_romaji,
        "host_name": contract.host_organization.name,
        "author": author,
    } | _ymd("made", on)
    placement = inputs.get("placement")
    if placement in (YES, NO):
        data |= {"placement_yes": placement == YES, "placement_no": placement == NO}
    if placement == YES:
        kind = inputs.get("placement_kind")
        if kind in ("paid", "free"):
            data |= {"placement_paid": kind == "paid", "placement_free": kind == "free"}
        data |= _ymd("license", _day(inputs.get("license_accepted_on")))
        postal = inputs.get("placement_postal", "")
        for key in (
            "license_number",
            "placement_name",
            "placement_address",
            "placement_phone",
            "placement_applicant_amount",
            "placement_applicant_yen",
            "placement_applicant_purpose",
            "placement_host_amount",
            "placement_host_purpose",
        ):
            data[key] = inputs.get(key, "")
        data["placement_postal"] = postal
        agency = inputs.get("agency")
        if agency in (YES, NO):
            data |= {"agency_yes": agency == YES, "agency_no": agency == NO}
        if agency == YES:
            for key in (
                "agency_name",
                "agency_country",
                "agency_address",
                "agency_phone",
                "agency_applicant_amount",
                "agency_applicant_yen",
                "agency_applicant_purpose",
                "agency_host_amount",
                "agency_host_yen",
                "agency_host_purpose",
            ):
                data[key] = inputs.get(key, "")
    guidance = inputs.get("guidance")
    if guidance in (YES, NO):
        data["guidance"] = "有" if guidance == YES else "無"
    total = 0
    for number in range(1, PAID_ROWS + 1):
        day = _day(inputs.get(f"paid{number}_date"))
        data |= {
            f"paid{number}_name": inputs.get(f"paid{number}_name", ""),
            f"paid{number}_purpose": inputs.get(f"paid{number}_purpose", ""),
            f"paid{number}_date": japanese(day) if day else "",
            f"paid{number}_amount": inputs.get(f"paid{number}_amount", ""),
        }
        yen = inputs.get(f"paid{number}_yen")
        if isinstance(yen, int):
            data[f"paid{number}_yen"] = yen
            total += yen
    if total:
        data["paid_total_yen"] = total
    data = {key: value for key, value in data.items() if value not in ("", None)}
    data["inputs"] = {
        key: (value.isoformat() if isinstance(value, datetime.date) else value)
        for key, value in inputs.items()
    }
    return {f"a.{key}": value for key, value in data.items()}


OVERVIEW_OFFICERS = 5
OVERVIEW_YEARS = 3


def overview_officers(host: HostOrganization, on: datetime.date) -> list[HostOfficer]:
    """基準日に在任の役員（取り消していない・退任していない。登録の順）。"""
    return [
        officer
        for officer in host.officers.filter(voided_at__isnull=True)
        if (officer.appointed_on is None or officer.appointed_on <= on)
        and (officer.resigned_on is None or officer.resigned_on > on)
    ]


def overview_1_11_1(host: HostOrganization, on: datetime.date, author: str) -> dict[str, Any]:
    """第1-11-1号 特定技能所属機関概要書（T46b、ADR-0038）。

    1 所属役員は 5 人まで（多ければ規則が「別紙」を警告する）。2 決算状況は基準日までに終わった
    年度の新しい 3 つ。3 は基準日までの最新の申告の版の人数。行方不明者が 0 人なら「該当なし」、
    1 人以上なら責めに帰すべき事由の有無で印を付ける。4・5 は全部委託なので空のまま。
    """
    data: dict[str, Any] = {"host_name": SP + host.name, "author": author} | _ymd("made", on)
    for number, officer in enumerate(overview_officers(host, on)[:OVERVIEW_OFFICERS], start=1):
        data |= {
            f"officer{number}_kana": officer.name_kana,
            f"officer{number}_name": officer.name,
            f"officer{number}_position": officer.position,
        }
    years = list(
        host.financial_years.filter(voided_at__isnull=True, fiscal_year_end__lte=on)[
            :OVERVIEW_YEARS
        ]
    )
    for number, year in enumerate(years, start=1):
        data |= {
            f"sales_{number}": year.sales,
            f"ordinary_{number}": year.ordinary_profit,
            f"net_{number}": year.net_profit,
            f"assets_{number}": year.net_assets,
        }
    declaration = host.declarations.filter(voided_at__isnull=True, declared_on__lte=on).first()
    if declaration is not None:
        for name in (
            "left_japanese_voluntary",
            "left_japanese_involuntary",
            "left_foreign_voluntary",
            "left_foreign_involuntary",
        ):
            data[name] = getattr(declaration, name)
        for key, count in (
            ("ssw", declaration.missing_ssw),
            ("interns", declaration.missing_interns),
        ):
            data[f"missing_{key}"] = count
            if count is None:
                continue
            fault = bool(count) and declaration.missing_at_fault is True
            if count == 0 or declaration.missing_at_fault is not None:
                data[f"missing_{key}_fault"] = fault
                data[f"missing_{key}_no_fault"] = not fault
    data["officers_over"] = len(overview_officers(host, on)) > OVERVIEW_OFFICERS
    return data


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
