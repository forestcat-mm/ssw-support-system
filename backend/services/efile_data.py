"""電子届出の一括申込用ツールの 1 行の値（T50。ADR-0040 決定 7〜9）。

値はツールのセルにそのまま書く表示名（業務コードTBL の表示内容漢字と完全に一致する文字）と、日付
（年・月・日の 3 セル）。本人の欄は台帳から、届出の内容の欄は届出タスクから既定値を作り、行の入力
（画面で直した値）で上書きする。届出機関は受入れ機関（第3-x号）か自機関（第4-3号）。
"""

from __future__ import annotations

import datetime
import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from apps.contracts.models import EmploymentContract, TerminationReason
from apps.core.models import Organization
from apps.filings.models import Filing, FilingKind
from apps.hosts.models import (
    HostOrganization,
    SupportTerminationCategory,
    SupportTerminationReason,
)
from apps.workers.models import ForeignWorker, LatestResidenceRecord
from services import addresses, contracts, immigration_codes, incident_reports

# 届出タスクの種別 → ツール（様式 ID）
KIND_FORMS: dict[str, str] = {
    FilingKind.CONTRACT_CHANGE: "efile-3-1",
    FilingKind.CONTRACT_TERMINATION: "efile-3-1",
    FilingKind.CONTRACT_NEW: "efile-3-1",
    FilingKind.SUPPORT_PLAN_CHANGE: "efile-3-2",
    FilingKind.SUPPORT_CONTRACT_CHANGE: "efile-3-3",
    FilingKind.SUPPORT_CONTRACT_END_OR_CONCLUSION: "efile-3-3",
    FilingKind.ACCEPTANCE_DIFFICULTY: "efile-3-4",
    FilingKind.MISSING: "efile-3-4",
    FilingKind.STANDARD_NONCONFORMITY: "efile-3-5",
    FilingKind.SUPPORT_PLAN_DIFFICULTY: "efile-3-7",
    FilingKind.SUPPORT_DIFFICULTY: "efile-4-3",
}
FORM_IDS = (
    "efile-3-1",
    "efile-3-2",
    "efile-3-3",
    "efile-3-4",
    "efile-3-5",
    "efile-3-7",
    "efile-4-3",
)
ORGANIZATION_FORMS = frozenset({"efile-4-3"})  # 届出機関（報告機関）が自機関

# 雇用条件書の項番（変更の判定の item）→ 変更事項の大分類（A023 の表示名）
CHANGE_MAJORS = {
    "Ⅰ": "雇用契約期間",
    "Ⅱ": "就業の場所",
    "Ⅲ": "従事すべき業務の内容",
    "Ⅳ": "労働時間等",
    "Ⅴ": "休日",
    "Ⅵ": "休暇",
    "Ⅶ": "賃金",
    "Ⅷ": "退職に関する事項",
    "Ⅸ": "その他（社会保険・労働保険の加入状況，健康診断，帰国担保措置）",
}
# 変更の判定のコード（頭）→ 小分類（A024 の表示名）。当たらなければ空（画面で選ぶ）
CHANGE_MINORS: tuple[tuple[str, str], ...] = (
    ("period_shortened", "01.雇用契約期間"),
    ("renewal_", "02.契約の更新の有無"),
    ("site_changed", "05.事業所名"),
    ("categories_changed", "09.業務区分"),
    ("scheduled_hours_changed", "12.所定労働時間数"),
    ("one_year_variable_hours", "12.所定労働時間数"),
    ("overtime_changed", "14.所定時間外労働の有無"),
    ("annual_holidays_decreased", "15.定例日"),
    ("paid_leave_decreased", "17.年次有給休暇"),
    ("early_paid_leave_", "17.年次有給休暇"),
    ("wage_type_changed", "19.基本賃金"),
    ("base_wage_decreased", "19.基本賃金"),
    ("allowance_", "20.諸手当（時間外労働の割増賃金は除く）"),
    ("fixed_overtime_changed", "20.諸手当（時間外労働の割増賃金は除く）"),
    ("premium_rate_decreased", "21.所定時間外，休日又は深夜労働に対して支払われる割増賃金率"),
    ("payment_to_cash", "24.賃金支払方法"),
    ("deduction_", "25.労使協定に基づく賃金支払時の控除"),
    ("has_raise", "26.昇給"),
    ("has_bonus", "27.賞与"),
    ("has_retirement", "28.退職金"),
    ("resignation_changed", "30.自己都合退職の手続"),
    ("social_insurance_changed", "32.社会保険・労働保険の加入状況"),
    ("labor_insurance_", "32.社会保険・労働保険の加入状況"),
    ("health_check_interval_changed", "33.健康診断"),
)
# 雇用契約の終了理由 → (終了の事由, 事由の詳細)（第3-1号の別紙 L・M）
TERMINATIONS = {
    TerminationReason.EXPIRY: ("雇用契約の期間満了", "01.雇用契約の期間満了"),
    TerminationReason.VOLUNTARY: ("特定技能外国人の都合", "10.自己都合退職"),
    TerminationReason.JOB_CHANGE: ("特定技能外国人の都合", "10.自己都合退職"),
    TerminationReason.COMPANY: ("特定技能所属機関の都合", "02.経営上の都合"),
}
SUPPORT_TERMINATION_MAJORS = {
    SupportTerminationCategory.EXPIRY: "委託契約の期間満了",
    SupportTerminationCategory.HOST: "特定技能所属機関の都合による終了",
    SupportTerminationCategory.SUPPORT_ORGANIZATION: "登録支援機関の都合による終了",
}
SUPPORT_TERMINATION_MINORS = {
    SupportTerminationReason.EXPIRY: "期間満了",
    SupportTerminationReason.BUSINESS: "経営上の都合",
    SupportTerminationReason.BREACH: "契約違反",
    SupportTerminationReason.REGISTRATION_REVOKED: "登録取消し",
    SupportTerminationReason.OTHER: "その他",
}
# 既定値が推定の欄（画面で「確かめる」を出す）
ESTIMATED = "estimated"


def form_of(filing: Filing) -> str:
    return KIND_FORMS.get(filing.kind, "")


def _code_name(table: Mapping[str, Any], code_type: str, code: str) -> str:
    for display, value in table["codes"].get(code_type, []):
        if value == code:
            return str(display)
    return ""


def _plain(text: str) -> str:
    return re.sub(r"[（）()\s]", "", unicodedata.normalize("NFKC", text))


def _digits(text: str) -> str:
    return re.sub(r"\D", "", unicodedata.normalize("NFKC", text or ""))


def contract_of(filing: Filing) -> EmploymentContract | None:
    target = filing.target
    if isinstance(target, EmploymentContract):
        return target
    if filing.worker is None:
        return None
    return contracts.contract_on(filing.worker, filing.basis_on)


def host_of(filing: Filing) -> HostOrganization | None:
    contract = contract_of(filing)
    return contract.host_organization if contract else None


# --- 本人の欄 ---------------------------------------------------------------------


def person_values(
    worker: ForeignWorker,
    contract: EmploymentContract | None,
    on: datetime.date,
    table: Mapping[str, Any],
) -> dict[str, Any]:
    nationalities = {display for display, _code in table["codes"].get("A103", [])}
    residence = LatestResidenceRecord.objects.select_related("record").filter(worker=worker).first()
    values: dict[str, Any] = {
        "p.name": unicodedata.normalize("NFKC", worker.name_romaji).upper(),
        "p.sex": {"male": "男", "female": "女"}.get(worker.sex, ""),
        "p.nationality": worker.nationality.name
        if worker.nationality.name in nationalities
        else "",
        "p.birth_date": worker.birth_date,
        "p.card": residence.record.card_number if residence else "",
    }
    version = contracts.version_on(contract, on) if contract else None
    if version is not None:
        field = version.site.field.name
        fields = {display for display, _code in table["codes"].get("CB54", [])}
        values["p.field"] = field if field in fields else ""
        category = version.categories.first()
        rows = [display for display, _code in table["codes"].get("CB55", [])]
        in_field = [row for row in rows if row.endswith(f"（{field}）")]
        if category is not None:
            wanted = _plain(category.name)
            matched = next((row for row in in_field if _plain(row).startswith(wanted)), "")
            if not matched and len(in_field) == 1 and "飲食料品" in field:
                # ツールに「飲食料品製造業全般」しか無い（ADR-0038・ADR-0039 決定 9）
                matched = in_field[0]
            values["p.category"] = matched
    return values


# --- 届出機関の欄・登録支援機関の欄 ----------------------------------------------------


def _address_block(postal_code: str, address: str) -> str:
    postal = _digits(postal_code)
    head = f"〒{postal[:3]}-{postal[3:]}" if len(postal) == 7 else ""
    return f"{head}\n{address}" if head else address


def organization_values(form_id: str, host: HostOrganization | None) -> dict[str, str]:
    if form_id in ORGANIZATION_FORMS:
        organization = Organization.objects.first()
        if organization is None:
            return {}
        return {
            "org.corporate_number": organization.corporate_number,
            "org.name": organization.name,
            "org.address": _address_block(organization.postal_code, organization.address),
        }
    if host is None:
        return {}
    return {
        "org.corporate_number": host.corporate_number,
        "org.name": host.name,
        "org.address": _address_block(host.postal_code, host.address),
    }


def support_organization_values() -> dict[str, str]:
    """第3-3号の登録支援機関の欄（自機関）。"""
    organization = Organization.objects.first()
    if organization is None:
        return {}
    registration = re.match(
        r"^\s*(\d+)\s*登\s*[-－ー―]?\s*(\d+)\s*$",
        unicodedata.normalize("NFKC", organization.registration_number),
    )
    split = addresses.split(organization.address)
    prefecture = (
        dict(immigration_codes.table(immigration_codes.PREFECTURE)).get(split.prefecture_code, "")
        if split.ok
        else ""
    )
    city = (
        addresses.municipality_name(split.prefecture_code, split.municipality_code)
        if split.ok
        else ""
    )
    return {
        "s.registration_prefix": f"{int(registration.group(1)):02d}" if registration else "",
        "s.registration_number": f"{int(registration.group(2)):06d}" if registration else "",
        "s.corporate_number": organization.corporate_number,
        "s.name": organization.name,
        "s.postal_code": _digits(organization.postal_code),
        "s.prefecture": prefecture,
        "s.city": city,
        "s.street": split.rest if split.ok else organization.address,
    }


# --- 届出の内容の欄（届出タスクからの既定値）--------------------------------------------


def change_lines(filing: Filing) -> list[dict[str, str]]:
    """雇用契約の変更の届出タスクの変更事項（大分類・小分類）。

    1 つの変更事項ごとに 1 行（T50-D10）。
    """
    details = filing.trigger_details or {}
    lines: list[dict[str, str]] = []
    for finding in list(details.get("required", [])) + list(details.get("review", [])):
        major = CHANGE_MAJORS.get(finding.get("item", ""), "")
        code = finding.get("code", "")
        minor = next((name for prefix, name in CHANGE_MINORS if code.startswith(prefix)), "")
        line = {"e.change_major": major, "e.change_minor": minor}
        if line not in lines:
            lines.append(line)
    return lines or [{}]


def event_defaults(
    filing: Filing, form_id: str, table: Mapping[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """届出タスクから作る届出の内容の既定値と、推定の欄のキー。"""
    kind = FilingKind(filing.kind)
    details = filing.trigger_details or {}
    on = filing.basis_on
    values: dict[str, Any] = {}
    estimated: list[str] = []
    if form_id == "efile-3-1":
        code = {
            FilingKind.CONTRACT_CHANGE: "01",
            FilingKind.CONTRACT_TERMINATION: "02",
            FilingKind.CONTRACT_NEW: "03",
        }[kind]
        values |= {"e.reason": _code_name(table, "A022", code), "e.date": on}
        if kind is FilingKind.CONTRACT_TERMINATION:
            end = TERMINATIONS.get(details.get("termination_reason", ""))
            if end:
                values |= {"e.end_reason": end[0], "e.end_detail": end[1]}
            values["e.end_other"] = details.get("termination_note", "")
        if kind is FilingKind.CONTRACT_NEW:
            contract = contract_of(filing)
            version = contracts.version_on(contract, on) if contract else None
            if version is not None:
                ends = f"{version.ends_on:%Y年%-m月%-d日}" if version.ends_on else ""
                values["e.new_content"] = f"雇用契約期間 {version.starts_on:%Y年%-m月%-d日}～{ends}"
    elif form_id == "efile-3-2":
        values["e.date"] = on
        item = details.get("plan_change_item")
        if item in ("III-6", "III-7"):
            # 支援委託契約の締結・終了で登録支援機関の事項がすべて変わる（推定）
            values |= {"e.change_major": "登録支援機関", "e.change_minor": "33.10～18　全ての変更"}
            estimated += ["e.change_major", "e.change_minor"]
            if item == "III-6":
                organization = Organization.objects.first()
                values["e.change_content"] = organization.name if organization else ""
    elif form_id == "efile-3-3":
        values["e.date"] = on
        event = details.get("event")
        if kind is FilingKind.SUPPORT_CONTRACT_CHANGE:
            values["e.reason"] = _code_name(table, "A064", "02")
            changes = details.get("changes") or {}
            if "monthly_fee" in changes:
                values |= {
                    "e.change_item": "02.委託料（１名あたりの月額）",
                    "e.change_content": f"月額{int(changes['monthly_fee'][1]):,}円",
                }
            elif changes:
                values["e.change_item"] = "05.契約期間等"
        elif event == "concluded":
            values |= {
                "e.reason": _code_name(table, "A064", "01"),
                "e.conclusion_content": "支援計画の全部の実施の委託",
            }
        elif event == "terminated":
            values |= {
                "e.reason": _code_name(table, "A064", "03"),
                "e.end_major": SUPPORT_TERMINATION_MAJORS.get(
                    details.get("termination_category", ""), ""
                ),
                "e.end_minor": SUPPORT_TERMINATION_MINORS.get(
                    details.get("termination_reason", ""), ""
                ),
            }
            if details.get("termination_reason") == SupportTerminationReason.OTHER:
                values["e.end_other"] = details.get("termination_note", "")
        values |= support_organization_values()
    elif form_id == "efile-3-4":
        values["e.date"] = on
        if kind is FilingKind.MISSING:
            values |= {
                "e.reason": "特定技能外国人の都合",
                "e.category": "行方不明",
                "e.state": "連絡不可能",
            }
        elif details.get("termination_reason") == TerminationReason.COMPANY:
            values |= {"e.reason": "特定技能所属機関の都合", "e.category": "経営上の都合"}
    elif form_id == "efile-3-5":
        values["e.known_on"] = on
    elif form_id in ("efile-3-7", "efile-4-3"):
        values["e.date"] = on
        if form_id == "efile-4-3" and any(
            "nonconformity" in source.reasons
            for source in incident_reports.interview_sources(filing)
        ):
            values |= {
                "e.reason": "その他の特異事案",
                "e.category": "特定技能所属機関における基準不適合等の問題を把握",
            }
            estimated += ["e.reason", "e.category"]
    return values, estimated


def defaults(filing: Filing, form_id: str, table: Mapping[str, Any]) -> list[dict[str, Any]]:
    """届出タスク 1 件の行の既定値（雇用契約の変更は変更事項ごとに複数の行）。"""
    values, estimated = event_defaults(filing, form_id, table)
    if form_id == "efile-3-1" and filing.kind == FilingKind.CONTRACT_CHANGE:
        return [values | line | {ESTIMATED: estimated} for line in change_lines(filing)]
    return [values | {ESTIMATED: estimated}]


# --- 1 行の値とセル --------------------------------------------------------------------


def jsonable(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value.isoformat() if isinstance(value, datetime.date) else value
        for key, value in values.items()
    }


def _as_date(value: Any) -> datetime.date | None:
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.date.fromisoformat(value)
        except ValueError:
            return None
    return None


def cells(definition: Mapping[str, Any], values: Mapping[str, Any]) -> dict[str, str]:
    """1 行の値 → 入力シートの列（日付は年 4 桁・月日 2 桁の文字。T50-D3）。届出機関の欄は除く。"""
    row: dict[str, str] = {}
    for field in definition["fields"]:
        target = field["target"]
        if target.startswith("org:"):
            continue
        value = values.get(field["key"])
        if field["format"] == "date3":
            day = _as_date(value)
            if day is None:
                continue
            year, month, date_ = target.split(",")
            row |= {year: f"{day.year:04d}", month: f"{day.month:02d}", date_: f"{day.day:02d}"}
            continue
        if value not in (None, ""):
            row[target] = str(value)
    return row


def header_cells(definition: Mapping[str, Any], values: Mapping[str, Any]) -> dict[str, str]:
    """届出機関の欄（セル番地 → 値）。"""
    table = definition["columns_table"]
    sheet = table["sheet"]
    result = {}
    for field in definition["fields"]:
        if field["target"].startswith("org:"):
            key = field["target"].removeprefix("org:")
            value = values.get(field["key"], "")
            if value:
                result[f"{sheet}!{table['organization_cells'][key]}"] = str(value)
    return result
