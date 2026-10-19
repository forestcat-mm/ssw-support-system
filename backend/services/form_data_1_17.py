"""支援計画書（参考様式第1-17号）のデータを、計画の版と台帳から組み立てる（T31、ADR-0020）。

- Ⅳ欄・Ⅴ欄・作成日・作成責任者・翻訳文の言語・面談の希望は、生成に使う計画の版（content_of）から
  取る。
- Ⅰ〜Ⅲ欄は計画に持たず、生成のときの台帳（外国人・計画の受入れ機関・自機関・事務所・担当者）から
  取る（ADR-0006「影響」）。使った値は生成履歴のスナップショット（暗号化）に残る。
- 欄への置き方（セル・書式・印刷済みの文への差し込み）は mappings/1-17/<適用開始日>.json に持つ。
  ここでは様式の言葉に合わせた値（「有」「無」「男」、言語の短い名前、時刻の書き方）だけを作る。
- 値が空の欄は書かない（印刷済みの文や □ を残す。T31-D4）。チェックは True が ■、None は書かない。
"""

from __future__ import annotations

import datetime
import re
from typing import Any

from apps.core.models import Office, Organization, Staff, SupportLanguage
from apps.support.models import (
    CheckMethod,
    ContactMethod,
    HousingTiming,
    InterviewPreference,
    MunicipalityPlace,
    PropertyType,
    SupportPlanVersion,
)
from apps.workers.models import ForeignWorker, Sex
from services import hosts, organization, support_plans
from services.support_catalog import ROWS, Method, SupportItem

ERASE = "　"  # 印刷済みの選択肢を消すときに書く全角空白（丸で囲む代わり。T31-D3）
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun", "holiday")
SEX_WORDS = {Sex.MALE: "男", Sex.FEMALE: "女"}
REGISTRATION_NUMBER = re.compile(r"^\s*(\S+?)\s*登\s*[-－ー―‐]\s*(\S+)\s*$")
POSTAL_CODE = re.compile(r"^\s*〒?\s*(\d{3})\s*[-－ー]?\s*(\d{4})\s*$")


def postal_parts(code: str) -> tuple[str, str]:
    """郵便番号を前 3 桁と後 4 桁に分ける（形が違えば全体を前に置く）。"""
    match = POSTAL_CODE.match(code or "")
    return (match.group(1), match.group(2)) if match else (code or "", "")


def registration_parts(number: str) -> tuple[str, str]:
    """登録番号「99登-999999」を「登」と「－」の前後に分ける（様式に「登」「－」が印刷済み）。"""
    match = REGISTRATION_NUMBER.match(number or "")
    return (match.group(1), match.group(2)) if match else (number or "", "")


def language_names(languages: list[SupportLanguage]) -> str:
    """様式の「〇〇語」の前に書く短い名前（複数は「・」でつなぐ。T31-D11）。"""
    return "・".join(language.form_name or language.name for language in languages)


def hour_text(value: str) -> str:
    """ "09:00" → "9時"、"09:30" → "9時30分"（様式は「時」が印刷済みでないため両方を書く）。"""
    hour, minute = (int(part) for part in value.split(":"))
    return f"{hour}時" if minute == 0 else f"{hour}時{minute}分"


def staff_label(staff: Staff | None) -> str:
    if staff is None:
        return ""
    return f"{staff.name}（{staff.position}）" if staff.position else staff.name


def office_and_managers(worker: ForeignWorker) -> tuple[Office | None, list[Staff]]:
    """Ⅲ欄６の事務所（担当者の事務所）と、Ⅲ欄７の支援責任者（その事務所で選んだ人。T31-D13、
    修正 1 回目 A5）。担当者がいなければ事務所も支援責任者も無い（ほかの事務所から選ばない）。"""
    staff = worker.assigned_staff
    office = staff.office if staff is not None else None
    manager = organization.support_manager(office)
    return office, [manager] if manager is not None else []


def supported_worker_count(worker: ForeignWorker, on: datetime.date) -> int:
    """Ⅲ欄７「支援を行っている1号特定技能外国人数」（予定の者〔この人〕を含む。T31-D14）。"""
    return len(hosts.supported_worker_ids(on) | {worker.pk})


def _row_values(code: str, activity: support_plans.ActivityContent) -> dict[str, Any]:
    prefix = f"plan.{code}."
    staff = Staff.objects.filter(pk=activity.staff_id).first() if activity.staff_id else None
    values: dict[str, Any] = {
        prefix + "planned_yes": True if activity.planned else None,
        prefix + "planned_no": True if not activity.planned else None,
        prefix + "yes_note": activity.planned_note if activity.planned else None,
        prefix + "no_note": activity.planned_note if not activity.planned else None,
        prefix + "outsourced": "有" if activity.outsourced else "無",
        prefix + "staff": (
            f"{activity.outsourcee_name}（{activity.outsourcee_title}）"
            if activity.outsourced and activity.outsourcee_title
            else activity.outsourcee_name
            if activity.outsourced
            else staff_label(staff)
        ),
        prefix + "address": activity.outsourcee_address if activity.outsourced else "",
        prefix + "content": activity.content,
        prefix + "method_text": activity.method_text,
        prefix + "note": activity.note,
    }
    for method in Method:
        values[prefix + f"method.{method.value}"] = True if method in activity.methods else None
        values[prefix + f"method_note.{method.value}"] = activity.method_notes.get(method, "")
    return values


def build(version: SupportPlanVersion, basis_on: datetime.date) -> dict[str, Any]:
    """生成に使うデータ（マッピングの key → 値）。"""
    plan = version.plan
    worker = plan.worker
    host = plan.host_organization
    organization = Organization.objects.first()
    content = support_plans.content_of(version)
    data: dict[str, Any] = {
        "plan.created_on": content.created_on,
        "plan.responsible_name": content.responsible_name,
        "plan.interview_online": content.interview_preference == InterviewPreference.ONLINE,
        "plan.interview_in_person": content.interview_preference == InterviewPreference.IN_PERSON,
    }
    if content.translation_language_id:
        language = SupportLanguage.objects.get(pk=content.translation_language_id)
        data["plan.translation_language"] = language_names([language])

    # Ⅰ 支援対象者
    if worker is not None:
        data |= {
            "worker.name_romaji": worker.name_romaji,
            "worker.sex": SEX_WORDS.get(Sex(worker.sex), "") if worker.sex else "",
            "worker.birth_date": worker.birth_date,
            "worker.nationality": worker.nationality.name if worker.nationality else "",
        }

    # Ⅱ 特定技能所属機関（Ⅱ欄３・４は全部委託では書かない〔注意２〕）
    if host is not None:
        first, last = postal_parts(host.postal_code)
        data |= {
            "host.name": host.name,
            "host.name_kana": host.name_kana,
            "host.postal_first": first,
            "host.postal_last": last,
            "host.address": host.address,
            "host.phone": host.phone_number,
        }

    # Ⅲ 登録支援機関（全部委託の場合だけ書く〔注意３〕。委託の範囲は全部委託だけ）
    if organization is not None:
        first, last = postal_parts(organization.postal_code)
        number_first, number_last = registration_parts(organization.registration_number)
        data |= {
            "organization.registration_first": number_first,
            "organization.registration_last": number_last,
            "organization.support_start_planned_on": organization.support_start_planned_on,
            "organization.name": organization.name,
            "organization.name_kana": organization.name_kana,
            "organization.postal_first": first,
            "organization.postal_last": last,
            "organization.address": organization.address,
            "organization.phone": organization.phone_number,
            "organization.representative_name": organization.representative_name,
            "organization.representative_name_kana": organization.representative_name_kana,
        }
    if worker is not None:
        office, managers = office_and_managers(worker)
        if office is not None:
            first, last = postal_parts(office.postal_code)
            data |= {
                "office.postal_first": first,
                "office.postal_last": last,
                "office.address": office.address,
                "office.phone": office.phone_number,
            }
        if managers:
            data |= {
                "manager.name": managers[0].name,
                "manager.name_kana": managers[0].name_kana,
                "manager.position": managers[0].position,
            }
        data["manager.count"] = len(managers)
        data["organization.worker_count"] = supported_worker_count(worker, basis_on)
    data["organization.staff_count"] = Staff.objects.filter(is_support_staff=True).count()

    # Ⅳ 支援の内容
    for row in ROWS:
        activity = content.activities.get(row.code)
        if activity is not None:
            data |= _row_values(row.code, activity)
    for item, item_content in content.items.items():
        languages = list(SupportLanguage.objects.filter(pk__in=item_content.language_ids))
        key = f"plan.item.{SupportItem(item).value}."
        data |= {
            key + "languages": language_names(languages),
            key + "interpreter": item_content.interpreter,
            key + "planned_hours": item_content.planned_hours,
        }
    housing = content.housing
    data |= {
        "housing.secured": True if housing.timing == HousingTiming.SECURED else None,
        "housing.after": True if housing.timing == HousingTiming.AFTER else None,
        "housing.occupants": housing.occupants,
        "housing.room_area_ok": True if housing.room_area_ok else None,
        "housing.bedroom_area_ok": True if housing.bedroom_area_ok else None,
        "housing.rent_yes": True if housing.rent_collected is True else None,
        "housing.rent_no": True if housing.rent_collected is False else None,
        "housing.monthly_rent": housing.monthly_rent,
        "housing.owned": True if housing.property_type == PropertyType.OWNED else None,
        "housing.leased": True if housing.property_type == PropertyType.LEASED else None,
        "housing.rent_explanation": housing.rent_explanation,
    }
    consultation = content.consultation
    for day in WEEKDAYS:
        hours = consultation.hours.get(day) or []
        if len(hours) == 2 and hours[0] and hours[1]:
            data[f"consultation.{day}.start"] = hour_text(hours[0])
            data[f"consultation.{day}.end"] = hour_text(hours[1])
    for prefix, methods, phone, email, other in (
        (
            "consultation",
            consultation.methods,
            consultation.phone,
            consultation.email,
            consultation.other,
        ),
        (
            "emergency",
            consultation.emergency_methods,
            consultation.emergency_phone,
            consultation.emergency_email,
            consultation.emergency_other,
        ),
    ):
        for method in ContactMethod:
            data[f"{prefix}.method.{method.value}"] = True if method in methods else None
        data |= {f"{prefix}.phone": phone, f"{prefix}.email": email, f"{prefix}.other": other}

    # Ⅴ 共生施策（実名を書く。「都・道・府・県」などを丸で囲む代わり。T31-D3）
    checks = {check.place: check for check in content.municipality_checks}
    data["municipality.checked"] = True if checks else None
    for place in MunicipalityPlace:
        check = checks.get(place)
        if check is None:
            continue
        key = f"municipality.{place.value}."
        data |= {
            key + "prefecture": check.prefecture,
            key + "municipality": check.municipality,
            key + "checked_on": check.checked_on,
            key + "website": True if check.method == CheckMethod.WEBSITE else None,
            key + "other": True if check.method == CheckMethod.OTHER else None,
            key + "method_note": check.method_note,
            key + "note": check.note,
        }
        if place == MunicipalityPlace.RESIDENCE:
            data[key + "same"] = True if check.same_as_site else None
            data[key + "different"] = None if check.same_as_site else True
    return data


def declaration_values(propriety: bool) -> dict[str, Any]:
    """生成の画面の申告（Ⅲ欄７「支援の適正性を確保していることの有無」。T31-D15）。

    申告したときは「有」を残して「無」を消す（丸で囲む代わり）。申告しないときは書かない。
    """
    if not propriety:
        return {"declaration.propriety": None, "declaration.propriety_no": None}
    return {"declaration.propriety": "有", "declaration.propriety_no": ERASE}
