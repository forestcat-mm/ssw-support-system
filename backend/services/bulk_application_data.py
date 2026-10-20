"""一括申請の 1 人分の値（データのキー → コードか文字。T48。ADR-0039）。

キーは tools/forms/make_bulk_mappings.py の欄の対応のキー。コードの欄（format=code）は、ツールの
業務コードの値を返す（表示名にするのは services/bulk_applications.py）。値が無い欄は空の文字にし、
生成前の検査（ツールの入力チェックの写し）が「必須」として知らせる。

台帳（T46a・T46b の項目・雇用契約・支援計画・担当者と支援責任者の履歴）から作り、台帳に無いものは
申請ごとの入力（BulkApplicationEntry.inputs と sensitive）から取る。
"""

from __future__ import annotations

import datetime
import json
import re
import unicodedata
from collections.abc import Iterable
from typing import Any

from apps.contracts.models import EmploymentContract, PaymentMethod
from apps.core.models import Office, Organization, Staff
from apps.forms_engine.models import BulkApplicationEntry, BulkProcedure
from apps.hosts.models import HostApplicantContact, HostOrganization, HostSite
from apps.support.models import Briefing, SupportPlan
from apps.workers.models import (
    ForeignWorker,
    LatestPassportRecord,
    LatestResidenceRecord,
    LatestWorkerAddress,
    LatestWorkerContact,
    QualificationArea,
    StatusOfResidence,
    TechnicalInternType,
)
from services import (
    addresses,
    contracts,
    host_declarations,
    hosts,
    immigration_codes,
    staff_periods,
    support_plans,
)
from services.support_catalog import SupportItem

YES, NO = "1", "0"  # A045・A021「有 Yes」「無 No」
ENTRUST, NOT_ENTRUST = "1", "0"  # A051
INTENTION_CONFIRMED = "1"  # A019
PROCEDURE_KEYS = {
    BulkProcedure.COE: "coe",
    BulkProcedure.EXTENSION: "extension",
    BulkProcedure.CHANGE: "change",
}
# 申請ごとの入力の既定（前の行が無いとき。T48-D1）
DEFAULT_INPUTS: dict[str, str] = {
    "hours_equal": "yes",
    "pay_equal": "yes",
    "discrimination": "no",
    "home_leave": "yes",
    "return_travel": "yes",
    "health": "yes",
    "plan_appropriate": "yes",
    "companion": "no",
    "intention": "yes",
}
SENSITIVE_KEYS = ("crime", "crime_detail")


def flag(value: Any) -> str:
    """真偽・「yes」「no」を A045 のコードに（空は空）。"""
    if value in (True, "yes"):
        return YES
    if value in (False, "no"):
        return NO
    return ""


def ymd(day: datetime.date | None) -> str:
    return day.strftime("%Y%m%d") if day else ""


def ym(day: datetime.date | None) -> str:
    return day.strftime("%Y%m") if day else ""


def digits(value: str | None) -> str:
    return re.sub(r"\D", "", unicodedata.normalize("NFKC", value or ""))


def sensitive_of(entry: BulkApplicationEntry) -> dict[str, str]:
    """犯罪を理由とする処分の有無と内容（暗号化の列。ADR-0038 決定 6）。"""
    if not entry.sensitive:
        return {}
    data: dict[str, str] = json.loads(entry.sensitive)
    return data


def has_crime(entry: BulkApplicationEntry) -> bool:
    return sensitive_of(entry).get("crime") == "yes"


def code_by_japanese(code: str, text: str, procedure: str) -> str:
    """ツールの表で、日本語の名前が text のコード（無ければ空）。"""
    for value, label in immigration_codes.procedure_table(code, procedure):
        if immigration_codes.japanese(label) == text:
            return value
    return ""


def _code_starting(code: str, prefixes: Iterable[str], procedure: str) -> str:
    for value, label in immigration_codes.procedure_table(code, procedure):
        if any(label.startswith(prefix) for prefix in prefixes):
            return value
    return ""


def _address(prefix: str, address: str) -> dict[str, str]:
    split = addresses.split(address)
    rest = split.rest if split.ok else ""
    return {
        f"{prefix}.pref": split.prefecture_code,
        f"{prefix}.city": split.municipality_code,
        f"{prefix}.address_rest": to_full_width(rest),
    }


def to_full_width(text: str) -> str:
    """半角の英数字・記号・空白を全角にする（全角だけの欄）。"""
    result = []
    for char in unicodedata.normalize("NFKC", text):
        if char == " ":
            result.append("　")
        elif "!" <= char <= "~":
            result.append(chr(ord(char) + 0xFEE0))
        else:
            result.append(char)
    return "".join(result)


def _latest(model: Any, worker: ForeignWorker) -> Any:
    latest = model._default_manager.select_related("record").filter(worker=worker).first()
    return getattr(latest, "record", None)


# --- 外国人 ------------------------------------------------------------------------------


def _status_code(record: Any, procedure: str) -> str:
    """現に有する在留資格（CB19）。技能実習はイ・ロで分かれる。"""
    status: str = record.status_of_residence if record else ""
    names: dict[str, tuple[str, ...]] = {
        StatusOfResidence.SSW_1: ("特定技能１号", "特定技能（１号）"),
        StatusOfResidence.SSW_2: ("特定技能２号", "特定技能（２号）"),
        StatusOfResidence.STUDENT: ("留学",),
        StatusOfResidence.DESIGNATED_TRANSITION: ("特定活動",),
    }
    if status in (StatusOfResidence.TECHNICAL_INTERN_2, StatusOfResidence.TECHNICAL_INTERN_3):
        number = "２" if status == StatusOfResidence.TECHNICAL_INTERN_2 else "３"
        kind = {TechnicalInternType.I: "イ", TechnicalInternType.RO: "ロ"}.get(
            record.technical_intern_type, ""
        )
        if not kind:
            return ""
        names[status] = (f"技能実習{number}号{kind}", f"技能実習（{number}号{kind}）")
    prefixes = names.get(status)
    return _code_starting("CB19", prefixes, procedure) if prefixes else ""


def worker_values(worker: ForeignWorker, procedure: str) -> dict[str, str]:
    residence = _latest(LatestResidenceRecord, worker)
    passport = _latest(LatestPassportRecord, worker)
    address = _latest(LatestWorkerAddress, worker)
    contact = _latest(LatestWorkerContact, worker)
    phone = digits(contact.phone if contact else "")
    mobile = phone if phone.startswith(("070", "080", "090")) else ""
    values: dict[str, str] = {
        "p.nationality": code_by_japanese("CA61", worker.nationality.name, procedure),
        "p.birth_date": ymd(worker.birth_date),
        "p.name": unicodedata.normalize("NFKC", worker.name_romaji).upper(),
        "p.sex": {"male": "1", "female": "2"}.get(worker.sex, ""),
        "p.birthplace": worker.birthplace,
        "p.home_address": to_full_width(contact.home_country_address if contact else ""),
        "p.postal": digits(address.postal_code if address else ""),
        "p.phone": "" if mobile else phone,
        "p.mobile": mobile,
        "p.email": contact.email if contact else "",
        "p.passport": passport.number if passport else "",
        "p.passport_expiry": ymd(passport.expires_on) if passport else "",
        "p.relatives": flag(worker.has_relatives_in_japan),
    }
    if address is not None:
        values |= {
            "p.pref": address.prefecture_code,
            "p.city": address.municipality_code,
            "p.address_rest": to_full_width(address.address_rest),
        }
    if procedure != "coe" and residence is not None:
        values |= {
            "p.status": _status_code(residence, procedure),
            "p.period": residence.period_of_stay,
            "p.expiry": ymd(residence.expires_on),
            "p.has_card": flag(bool(residence.card_number)),
            "p.card": residence.card_number,
        }
    for number, relative in enumerate(worker.relatives.filter(voided_at__isnull=True)[:6], 1):
        values |= {
            f"rel{number}.relationship": relative.relationship,
            f"rel{number}.name": to_full_width(relative.name),
            f"rel{number}.birth_date": ymd(relative.birth_date),
            f"rel{number}.nationality": relative.nationality_code,
            f"rel{number}.together": flag(relative.lives_together),
            f"rel{number}.workplace": to_full_width(relative.workplace),
            f"rel{number}.card": relative.card_number,
        }
    values |= qualification_values(worker)
    values |= work_history_values(worker)
    return values


def qualification_values(worker: ForeignWorker) -> dict[str, str]:
    values: dict[str, str] = {}
    rows = list(worker.qualifications.filter(voided_at__isnull=True))
    for area, prefix in ((QualificationArea.SKILL, "skill"), (QualificationArea.JAPANESE, "ja")):
        own = [row for row in rows if row.area == area]
        if not own:
            continue
        values[f"q.{prefix}_eval"] = own[0].evaluation
        exams = [row for row in own if row.exam][:3]
        for number, row in enumerate(exams, 1):
            values[f"q.{prefix}_exam{number}"] = row.exam
            values[f"q.{prefix}_place{number}"] = row.exam_country
        other = next((row.other_proof for row in own if row.other_proof), "")
        values[f"q.{prefix}_other"] = other
    interns = list(worker.internship_completions.filter(voided_at__isnull=True)[:2])
    for number, intern in enumerate(interns, 1):
        values |= {
            f"q.intern{number}_occupation": intern.occupation,
            f"q.intern{number}_work": intern.work,
            f"q.intern{number}_proof": intern.proof,
        }
    return values


def ssw_months(worker: ForeignWorker, on: datetime.date) -> int:
    """特定技能1号での通算在留期間（月）。台帳の在留情報の行（特定技能1号）の期間と、台帳の外の申告。

    行ごとに許可日から次の行の許可日（無ければ満了日と基準日の早い方）までを数える（未確認の点）。
    """
    rows = list(worker.residence_records.filter(voided_at__isnull=True).order_by("permitted_on"))
    days = 0
    for index, row in enumerate(rows):
        if row.status_of_residence != StatusOfResidence.SSW_1:
            continue
        end = rows[index + 1].permitted_on if index + 1 < len(rows) else min(row.expires_on, on)
        days += max(0, (min(end, on) - row.permitted_on).days)
    return days * 12 // 365 + (worker.ssw_months_outside_ledger or 0)


def work_history_values(worker: ForeignWorker) -> dict[str, str]:
    values = {"w.has": flag(worker.has_work_history)}
    for number, row in enumerate(worker.work_history.filter(voided_at__isnull=True)[:10], 1):
        values |= {
            f"w{number}.joined": ym(row.joined_on),
            f"w{number}.left": ym(row.left_on),
            f"w{number}.employer": to_full_width(row.employer),
        }
    return values


# --- 雇用契約・受入れ機関・事業所 ----------------------------------------------------------


def _category_code(category_name: str, field_code: str, procedure: str) -> str:
    """業務区分（CC05。分野の連動の表）。表示名は「機械金属加工（工業製品製造業分野） …」の形。

    飲食料品製造業分野は、ツールに「飲食料品製造業全般」しか無い（2027-04-01 の 2 区分はまだ無い。
    ADR-0038）ので、その分野の区分はすべてそれに当てる。
    """
    rows = immigration_codes.procedure_linked("CC04", field_code, procedure) or []

    def plain(text: str) -> str:
        return re.sub(r"[（）()\s]", "", unicodedata.normalize("NFKC", text))

    wanted = plain(category_name)
    for value, label in rows:
        if plain(label).startswith(wanted):
            return value
    if len(rows) == 1 and "飲食料品" in label_of_field(field_code, procedure):
        return rows[0][0]
    return ""


def label_of_field(field_code: str, procedure: str) -> str:
    return immigration_codes.full_label("CC04", field_code, procedure)


def contract_values(
    contract: EmploymentContract, on: datetime.date, procedure: str
) -> dict[str, str]:
    version = contracts.version_on(contract, on)
    if version is None:
        return {}
    field_code = code_by_japanese("CC04", version.site.field.name, procedure)
    values: dict[str, str] = {
        "c.starts_on": ymd(version.starts_on),
        "c.ends_on": ymd(version.ends_on),
        "c.field1": field_code,
        "c.weekly_hours": str(round((version.weekly_working_minutes or 0) / 60))
        if version.weekly_working_minutes
        else "",
        "c.monthly_hours": str(round((version.monthly_working_minutes or 0) / 60))
        if version.monthly_working_minutes
        else "",
        "c.monthly_pay": str(contracts.monthly_total(version) or ""),
        "c.hourly_pay": str(_hourly(version) or ""),
        "c.payment_method": {
            str(PaymentMethod.CASH): "01",
            str(PaymentMethod.BANK_TRANSFER): "02",
        }.get(version.payment_method, ""),
    }
    for number, category in enumerate(version.categories.all()[:3], 1):
        values[f"c.category{number}"] = _category_code(category.name, field_code, procedure)
    explanation = getattr(version, "wage_explanation", None)
    if explanation is not None and explanation.japanese_wage and explanation.wage_unit == "monthly":
        values["c.japanese_pay"] = str(explanation.japanese_wage)
    values |= host_values(contract.host_organization, version.site, on, procedure)
    return values


def _hourly(version: Any) -> int | None:
    """基本給の時間換算額（版の値。無ければ月額 ÷ 月の所定労働時間。端数は切り捨て）。"""
    if version.hourly_equivalent:
        return int(version.hourly_equivalent)
    monthly = version.base_wage if version.wage_type == "monthly" else version.monthly_equivalent
    if monthly and version.monthly_working_minutes:
        return int(monthly * 60 // version.monthly_working_minutes)
    return None


def host_values(
    host: HostOrganization, site: HostSite, on: datetime.date, procedure: str
) -> dict[str, str]:
    year = host.financial_years.filter(voided_at__isnull=True, fiscal_year_end__lte=on).first()
    values: dict[str, str] = {
        "h.name": to_full_width(host.name),
        "h.has_corporate_number": flag(bool(host.corporate_number)),
        "h.corporate_number": host.corporate_number,
        "h.employment_insurance_number": site.employment_insurance_number,
        "h.industry1": host.industry_1,
        "h.industry1_other": host.industry_1_other,
        "h.industry2": host.industry_2,
        "h.industry2_other": host.industry_2_other,
        "h.industry3": host.industry_3,
        "h.industry3_other": host.industry_3_other,
        "h.postal": digits(host.postal_code),
        "h.phone": digits(host.phone_number),
        "h.capital": str(host.capital) if host.capital is not None else "",
        "h.sales": str(year.sales) if year else "",
        "h.employees": str(site.full_time_employees)
        if site.full_time_employees is not None
        else "",
        "h.representative": to_full_width(host.representative_name),
        "s.name": to_full_width(site.name),
        "s.postal": digits(site.postal_code),
        "s.health_pension": flag(site.health_pension_insured),
        "s.labour_insurance": flag(site.labour_insurance_covered),
        "s.labour_insurance_number": site.labour_insurance_number,
        "h.full_support": ENTRUST if hosts.active_contract(host, on) else NOT_ENTRUST,
    }
    values |= _address("h", host.address) | _address("s", site.address)
    declaration = host.declarations.filter(voided_at__isnull=True, declared_on__lte=on).first()
    if declaration is not None:
        for item in host_declarations.items():
            answer = (declaration.answers or {}).get(item.key, {})
            values[f"decl.{item.key}"] = flag(answer.get("answer"))
            if item.detail:
                values[f"decl.{item.key}_detail"] = answer.get("detail", "")
    return values


def agent_values(agent: HostApplicantContact | None) -> dict[str, str]:
    if agent is None:
        return {}
    host = agent.host_organization
    address = agent.address or host.address
    return {
        "ag.name": to_full_width(agent.name),
        "ag.relationship": to_full_width(agent.relationship),
        "ag.postal": digits(agent.postal_code or host.postal_code),
        "ag.phone": digits(agent.phone_number),
        "ag.mobile": digits(agent.mobile_number),
    } | _address("ag", address)


def intermediary_values(staff: Staff | None) -> dict[str, str]:
    if staff is None:
        return {}
    organization = Organization.objects.first()
    office: Office | None = staff.office
    values = {
        "it.name": to_full_width(staff.name),
        "it.organization": to_full_width(organization.name if organization else ""),
        "it.postal": digits(office.postal_code if office else ""),
        "it.phone": digits(office.phone_number if office else ""),
    }
    return values | _address("it", office.address if office else "")


# --- 支援計画・登録支援機関 ------------------------------------------------------------------

# 区分V の支援計画の欄 → 支援計画の様式の節（services/support_catalog.ROWS の符号の頭）
PLAN_SECTIONS = {
    "plan.guidance": ("1",),
    "plan.guidance_method": ("1",),
    "plan.transfer": ("2",),
    "plan.housing": ("3a",),
    "plan.contracts": ("3b",),
    "plan.orientation": ("4",),
    "plan.procedures": ("4",),  # 関係機関への同行（様式の 4 の届出・手続の項目。T48）
    "plan.japanese": ("5",),
    "plan.consultation": ("6",),
    "plan.exchange": ("7",),
    "plan.job_change": ("8",),
    "plan.interview": ("9",),
}


def plan_values(worker: ForeignWorker, host: HostOrganization, on: datetime.date) -> dict[str, str]:
    plan = SupportPlan.objects.filter(worker=worker, host_organization=host).first()
    version = support_plans.current_version(plan, on) if plan else None
    version = version or (support_plans.latest_version(plan) if plan else None)
    if version is None:
        return {}
    planned = {
        activity.code.split(".")[0] for activity in version.activities.all() if activity.planned
    }
    values = {
        key: flag(any(section in planned for section in sections))
        for key, sections in PLAN_SECTIONS.items()
    }
    values["plan.copy"] = YES
    return values


def organization_values(
    worker: ForeignWorker, host: HostOrganization, on: datetime.date
) -> dict[str, str]:
    organization = Organization.objects.first()
    if organization is None:
        return {}
    staff = staff_periods.worker_staff_on(worker, on).staff
    office = staff.office if staff else None
    manager = (
        staff_periods.staff_on(
            staff_periods.office_periods(office), on, fallback=office.support_manager
        ).staff
        if office
        else None
    )
    support = hosts.active_contract(host, on)
    values = {
        "o.name": to_full_width(organization.name),
        "o.has_corporate_number": flag(bool(organization.corporate_number)),
        "o.corporate_number": organization.corporate_number,
        "o.employment_insurance_number": organization.employment_insurance_number,
        "o.postal": digits(organization.postal_code),
        "o.phone": digits(organization.phone_number),
        "o.representative": to_full_width(organization.representative_name),
        "o.registration_number": registration_number(organization.registration_number),
        "o.registered_on": ymd(organization.registered_on),
        "of.name": to_full_width(office.name if office else ""),
        "of.postal": digits(office.postal_code if office else ""),
        "of.manager": to_full_width(manager.name if manager else ""),
        "of.staff": to_full_width(staff.name if staff else ""),
        "of.languages": "、".join(
            language.name for language in (staff.languages.all() if staff else [])
        ),
        "o.monthly_fee": str(support.monthly_fee) if support and support.monthly_fee else "",
    }
    values |= _address("o", organization.address)
    values |= _address("of", office.address if office else "")
    return values


def registration_number(text: str) -> str:
    """登録番号（「19登-000123」→「１９登０００１２３」。漢字と全角数字の 9 字）。"""
    match = re.match(r"^\s*(\d+)\s*登\s*[-－ー―]?\s*(\d+)\s*$", unicodedata.normalize("NFKC", text))
    if match is None:
        return to_full_width(text)
    return to_full_width(f"{int(match.group(1)):02d}登{int(match.group(2)):06d}")


# --- 保証金と費用（事前ガイダンスの記録か、申請ごとの入力）---------------------------------------


def fee_values(worker: ForeignWorker, inputs: dict[str, Any]) -> dict[str, str]:
    briefing = (
        Briefing.objects.filter(
            worker=worker,
            record__kind=SupportItem.PRE_GUIDANCE,
            record__voided_at__isnull=True,
        )
        .prefetch_related("fees")
        .first()
    )
    if briefing is not None and briefing.deposit_contract:
        values = {
            "f.deposit": flag(briefing.deposit_contract == "exists"),
            "f.deposit_collector": briefing.deposit_collector,
            "f.deposit_amount": briefing.deposit_detail,
        }
        if briefing.foreign_fees:
            fees = list(briefing.fees.all())
            values["f.foreign_fee"] = flag(
                briefing.foreign_fees == "paid" and briefing.fees_agreed is not False
            )
            if fees:
                values["f.foreign_fee_org"] = "、".join(fee.organization_name for fee in fees)
                values["f.foreign_fee_amount"] = f"{sum(fee.amount_jpy for fee in fees)}円"
        return values
    return {
        "f.deposit": flag(inputs.get("deposit")),
        "f.deposit_collector": inputs.get("deposit_collector", ""),
        "f.deposit_amount": inputs.get("deposit_amount", ""),
        "f.foreign_fee": flag(inputs.get("foreign_fee")),
        "f.foreign_fee_org": inputs.get("foreign_fee_org", ""),
        "f.foreign_fee_amount": inputs.get("foreign_fee_amount", ""),
    }


# --- 申請ごとの入力 -------------------------------------------------------------------------


def input_values(entry: BulkApplicationEntry, procedure: str) -> dict[str, str]:
    inputs = {**DEFAULT_INPUTS, **(entry.inputs or {})}
    sensitive = sensitive_of(entry)
    values = {
        "a.spouse": flag(inputs.get("spouse")),
        "a.occupation": to_full_width(inputs.get("occupation", "")),
        "a.reason": inputs.get("reason", ""),
        "a.desired_period": inputs.get("desired_period", ""),
        "a.ed_alpha": inputs.get("ed_alpha", ""),
        "a.ed_number": inputs.get("ed_number", ""),
        "a.receipt": inputs.get("receipt", ""),
        "a.receipt_residence": inputs.get("receipt_residence", ""),
        "a.receipt_office": inputs.get("receipt_office", ""),
        "a.notice_email": inputs.get("notice_email", ""),
        "a.intention": INTENTION_CONFIRMED if inputs.get("intention") == "yes" else "",
        "a.free": inputs.get("free", ""),
        "d.crime": flag(sensitive.get("crime")),
        "d.crime_detail": sensitive.get("crime_detail", ""),
        "c.main_occupation": inputs.get("main_occupation", ""),
        "c.other_occupation1": inputs.get("other_occupation1", ""),
        "c.other_occupation2": inputs.get("other_occupation2", ""),
        "c.other_occupation3": inputs.get("other_occupation3", ""),
        "c.hours_equal": flag(inputs.get("hours_equal")),
        "c.pay_equal": flag(inputs.get("pay_equal")),
        "c.discrimination": flag(inputs.get("discrimination")),
        "c.discrimination_detail": inputs.get("discrimination_detail", ""),
        "c.home_leave": flag(inputs.get("home_leave")),
        "c.return_travel": flag(inputs.get("return_travel")),
        "c.health": flag(inputs.get("health")),
        "plan.appropriate": flag(inputs.get("plan_appropriate")),
    }
    if procedure == "coe":
        values |= {
            "a.entry_purpose": _code_starting("CC11", ("特定技能（１号）",), procedure),
            "a.entry_month": inputs.get("entry_month", ""),
            "a.port": inputs.get("port", ""),
            "a.port_other": inputs.get("port_other", ""),
            "a.stay_years": inputs.get("stay_years", ""),
            "a.stay_months": inputs.get("stay_months", ""),
            "a.companion": flag(inputs.get("companion")),
            "a.visa_place": inputs.get("visa_place", ""),
            "d.entry_history": flag(inputs.get("entry_history")),
            "d.entry_count": inputs.get("entry_count", ""),
            "d.last_entry_from": inputs.get("last_entry_from", ""),
            "d.last_entry_to": inputs.get("last_entry_to", ""),
            "d.coe_history": flag(inputs.get("coe_history")),
            "d.coe_count": inputs.get("coe_count", ""),
            "d.coe_refused": inputs.get("coe_refused", ""),
            "d.deport": flag(inputs.get("deport")),
            "d.deport_count": inputs.get("deport_count", ""),
            "d.deport_last": inputs.get("deport_last", ""),
        }
    if procedure == "change":
        values["a.desired_status"] = _code_starting(
            "CB19_5", ("特定技能１号", "特定技能（１号）"), procedure
        )
    if procedure != "coe":
        values |= {"sim.reentry": NO, "sim.extra_activity": NO, "sim.work_certificate": NO}
    return values


def contract_of(worker: ForeignWorker, on: datetime.date) -> EmploymentContract | None:
    return contracts.contract_on(worker, on)


def build(entry: BulkApplicationEntry) -> dict[str, str]:
    """1 人分の値。"""
    application = entry.application
    procedure = PROCEDURE_KEYS[BulkProcedure(application.procedure)]
    on = application.basis_on
    worker = entry.worker
    values = worker_values(worker, procedure)
    months = ssw_months(worker, on)
    values |= {"q.ssw_years": str(min(months // 12, 5)), "q.ssw_months": str(months % 12)}
    contract = contract_of(worker, on)
    if contract is not None:
        values |= contract_values(contract, on, procedure)
        values |= plan_values(worker, contract.host_organization, on)
        values |= organization_values(worker, contract.host_organization, on)
    values |= agent_values(entry.agent)
    values |= intermediary_values(application.intermediary)
    values |= fee_values(worker, entry.inputs or {})
    values |= input_values(entry, procedure)
    return values


def default_inputs(worker: ForeignWorker) -> dict[str, str]:
    """新しい行の入力の初期値（同じ外国人の前の行の答え。無ければ既定）。"""
    previous = BulkApplicationEntry.objects.filter(worker=worker).order_by("-pk").first()
    return {**DEFAULT_INPUTS, **(previous.inputs if previous else {})}
