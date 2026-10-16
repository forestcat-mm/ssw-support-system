"""支援計画（第5章 F04、T16）。版の作成・標準の計画・検査・差分。

計画の中身は `PlanContent`（データだけの入れ物）で受け渡しする。版はこの中身から
1 つのトランザクションで作り、作成後は変更しない（訂正も新しい版）。
標準の計画も同じ形の見出し（is_template）で持ち、変更は新しい版として残す
（いつ・誰が標準を変えたかが追える）。
"""

from __future__ import annotations

import datetime
from dataclasses import asdict, dataclass, field, fields, replace
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.models import Staff, SupportLanguage, User
from apps.hosts.models import HostOrganization
from apps.support.models import (
    SupportPlan,
    SupportPlanActivity,
    SupportPlanConsultation,
    SupportPlanHousing,
    SupportPlanItem,
    SupportPlanMunicipalityCheck,
    SupportPlanVersion,
)
from apps.workers.models import ForeignWorker
from services.support_catalog import (
    ITEMS_WITH_HOURS,
    METHODS_WITH_NOTE,
    ROWS,
    ROWS_BY_CODE,
    SupportItem,
)

# 標準の計画の実施予定の初期値（記載例の書き方に合わせる。設定画面で直せる）
DEFAULT_TIMING: dict[str, str] = {
    "1.main": "在留資格認定証明書交付申請前（在留資格変更許可申請前）",
    "2.a": "入国時",
    "2.b": "契約終了後適宜実施",
    "3a.a": "入国後速やかに",
    "3b.a": "入国後速やかに",
    "3b.b": "入国後速やかに",
    "3b.c": "入国後速やかに",
    "4.main": "入国後（就労開始前）",
    "5.a": "入国後随時",
    "5.b": "入国後随時",
    "6.main": "適宜実施",
    "7.main": "随時",
    "8.a": "非自発的離職時",
    "8.b": "非自発的離職時",
    "8.c": "非自発的離職時",
    "8.f": "非自発的離職時",
    "9.main": "3か月に1回以上",
}


@dataclass
class ItemContent:
    language_ids: list[int] = field(default_factory=list)
    interpreter: str = ""
    planned_hours: Decimal | None = None


@dataclass
class ActivityContent:
    planned: bool = True
    planned_note: str = ""
    content: str = ""
    outsourced: bool = False
    staff_id: int | None = None
    outsourcee_name: str = ""
    outsourcee_title: str = ""
    outsourcee_address: str = ""
    methods: list[str] = field(default_factory=list)
    method_notes: dict[str, str] = field(default_factory=dict)
    method_text: str = ""
    note: str = ""


@dataclass
class HousingContent:
    timing: str = ""
    room_area_ok: bool = False
    occupants: int | None = None
    bedroom_area_ok: bool = False
    rent_collected: bool | None = None
    monthly_rent: int | None = None
    property_type: str = ""
    rent_explanation: str = ""


@dataclass
class ConsultationContent:
    hours: dict[str, list[str]] = field(default_factory=dict)
    methods: list[str] = field(default_factory=list)
    phone: str = ""
    email: str = ""
    other: str = ""
    emergency_methods: list[str] = field(default_factory=list)
    emergency_phone: str = ""
    emergency_email: str = ""
    emergency_other: str = ""


@dataclass
class MunicipalityContent:
    place: str = ""
    same_as_site: bool = False
    prefecture: str = ""
    municipality: str = ""
    checked_on: datetime.date | None = None
    method: str = ""
    method_note: str = ""
    note: str = ""


@dataclass
class PlanContent:
    """計画の版の中身（10項目・細目・住居・相談の受付・共生施策の確認・計画全体の欄）。"""

    items: dict[str, ItemContent] = field(default_factory=dict)
    activities: dict[str, ActivityContent] = field(default_factory=dict)
    housing: HousingContent = field(default_factory=HousingContent)
    consultation: ConsultationContent = field(default_factory=ConsultationContent)
    municipality_checks: list[MunicipalityContent] = field(default_factory=list)
    created_on: datetime.date | None = None
    responsible_name: str = ""
    translation_language_id: int | None = None
    interview_preference: str = ""


def default_content() -> PlanContent:
    """標準の計画がまだ無いときの初期値。すべての細目を「有」、自由記入の行は「無」。"""
    activities = {
        row.code: ActivityContent(
            planned=not row.is_free, planned_note=DEFAULT_TIMING.get(row.code, "")
        )
        for row in ROWS
    }
    return PlanContent(
        items={item.value: ItemContent() for item in SupportItem},
        activities=activities,
    )


# --- 検査 -----------------------------------------------------------------------


def validate(content: PlanContent) -> None:
    """様式の注意書き（参考様式第1-17号）に沿った検査。エラーは「欄の場所: 文言」の dict。"""
    errors: dict[str, list[str]] = {}

    def add(key: str, message: str) -> None:
        errors.setdefault(key, []).append(message)

    if set(content.items) != {item.value for item in SupportItem}:
        add("items", _("All ten support items are required."))
    if set(content.activities) != set(ROWS_BY_CODE):
        add("activities", _("The rows of the support plan do not match the form."))
    for item, item_content in content.items.items():
        if item_content.planned_hours is not None and item not in ITEMS_WITH_HOURS:
            add(
                f"item:{item}:planned_hours",
                _("Planned hours are only for guidance and orientation."),
            )
    for code, activity in content.activities.items():
        row = ROWS_BY_CODE.get(code)
        if row is None:
            continue
        if row.is_free and not activity.planned:
            continue  # 使わない自由記入の行
        if row.is_free and not activity.content.strip():
            add(f"activity:{code}:content", _("Enter the support content."))
        if not activity.planned and not activity.planned_note.strip():
            # 注意 4: 「無」の場合は実施しない理由を書く
            add(f"activity:{code}:planned_note", _("Enter the reason for not implementing."))
        if activity.outsourced and not (
            activity.outsourcee_name.strip() and activity.outsourcee_address.strip()
        ):
            # 注意 6: 委託を受けた第三者の氏名と所在地
            add(
                f"activity:{code}:outsourcee_name",
                _("Enter the name and address of the contractor."),
            )
        unknown = set(activity.methods) - {method.value for method in row.methods}
        if unknown:
            add(f"activity:{code}:methods", _("Select methods from the form."))
        for method in activity.methods:
            if method in METHODS_WITH_NOTE and not activity.method_notes.get(method, "").strip():
                add(f"activity:{code}:method_notes", _("Fill in the details of the method."))
    sites = [check.place for check in content.municipality_checks]
    if len(sites) != len(set(sites)):
        add("municipality_checks", _("Enter each place only once."))
    if errors:
        raise ValidationError(errors)


# --- 版の作成 -------------------------------------------------------------------


def _next_version(plan: SupportPlan) -> int:
    latest = plan.versions.order_by("-version").first()
    return 1 if latest is None else latest.version + 1


def _check_version_rules(
    plan: SupportPlan, applies_from: datetime.date, change_reason: str
) -> None:
    latest = plan.versions.order_by("-version").first()
    if latest is None:
        return
    if not change_reason.strip():
        raise ValidationError({"change_reason": [_("Enter the reason for the change.")]})
    if applies_from < latest.applies_from:
        raise ValidationError(
            {"applies_from": [_("The date must be on or after the date of the previous version.")]}
        )


@transaction.atomic
def add_version(
    plan: SupportPlan,
    content: PlanContent,
    *,
    applies_from: datetime.date,
    change_reason: str = "",
    by: User | None,
) -> SupportPlanVersion:
    """版を足す（検査の後、中身をすべて 1 つのトランザクションで作る）。"""
    validate(content)
    _check_version_rules(plan, applies_from, change_reason)
    stamp = {"created_by": by, "updated_by": by}
    version = SupportPlanVersion.objects.create(
        plan=plan,
        version=_next_version(plan),
        applies_from=applies_from,
        change_reason=change_reason.strip(),
        created_on=content.created_on,
        responsible_name=content.responsible_name,
        translation_language_id=content.translation_language_id,
        interview_preference=content.interview_preference,
        **stamp,
    )
    for item in SupportItem:
        item_content = content.items[item.value]
        plan_item = SupportPlanItem.objects.create(
            version=version,
            item=item.value,
            interpreter=item_content.interpreter,
            planned_hours=item_content.planned_hours,
            **stamp,
        )
        plan_item.languages.set(SupportLanguage.objects.filter(pk__in=item_content.language_ids))
    for row in ROWS:
        activity = content.activities[row.code]
        SupportPlanActivity.objects.create(
            version=version, code=row.code, **asdict(activity), **stamp
        )
    SupportPlanHousing.objects.create(version=version, **asdict(content.housing), **stamp)
    SupportPlanConsultation.objects.create(version=version, **asdict(content.consultation), **stamp)
    for check in content.municipality_checks:
        SupportPlanMunicipalityCheck.objects.create(version=version, **asdict(check), **stamp)
    return version


def content_of(version: SupportPlanVersion) -> PlanContent:
    """版の中身を PlanContent に戻す（標準からの複製・新版の編集の元にする）。"""
    items = {
        row.item: ItemContent(
            language_ids=sorted(language.pk for language in row.languages.all()),
            interpreter=row.interpreter,
            planned_hours=row.planned_hours,
        )
        for row in version.items.prefetch_related("languages")
    }
    activities = {
        row.code: ActivityContent(
            planned=row.planned,
            planned_note=row.planned_note,
            content=row.content,
            outsourced=row.outsourced,
            staff_id=row.staff_id,
            outsourcee_name=row.outsourcee_name,
            outsourcee_title=row.outsourcee_title,
            outsourcee_address=row.outsourcee_address,
            methods=list(row.methods),
            method_notes=dict(row.method_notes),
            method_text=row.method_text,
            note=row.note,
        )
        for row in version.activities.all()
    }
    housing = getattr(version, "housing", None)
    consultation = getattr(version, "consultation", None)
    return PlanContent(
        items=items,
        activities=activities,
        housing=HousingContent(**_fields(housing, HousingContent)) if housing else HousingContent(),
        consultation=(
            ConsultationContent(**_fields(consultation, ConsultationContent))
            if consultation
            else ConsultationContent()
        ),
        municipality_checks=[
            MunicipalityContent(**_fields(check, MunicipalityContent))
            for check in version.municipality_checks.order_by("place")
        ],
        created_on=version.created_on,
        responsible_name=version.responsible_name,
        translation_language_id=version.translation_language_id,
        interview_preference=version.interview_preference,
    )


def _fields(instance: Any, content_class: Any) -> dict[str, Any]:
    return {spec.name: getattr(instance, spec.name) for spec in fields(content_class)}


# --- 標準の計画・計画の作成 -----------------------------------------------------------


def template_plan(*, by: User | None = None) -> SupportPlan:
    """自機関の標準の計画（無ければ初期値の版 1 で作る）。"""
    plan = SupportPlan.objects.filter(is_template=True).first()
    if plan is not None:
        return plan
    with transaction.atomic():
        plan = SupportPlan.objects.create(is_template=True, created_by=by, updated_by=by)
        add_version(plan, default_content(), applies_from=timezone.localdate(), by=by)
    return plan


def template_content() -> PlanContent:
    version = latest_version(template_plan())
    assert version is not None
    return content_of(version)


def update_template(
    content: PlanContent, *, change_reason: str, by: User | None
) -> SupportPlanVersion:
    """標準の計画を変える（新しい版として残す）。"""
    plan = template_plan(by=by)
    return add_version(
        plan, content, applies_from=timezone.localdate(), change_reason=change_reason, by=by
    )


def content_for_worker(worker: ForeignWorker) -> PlanContent:
    """標準の計画を複製し、担当が空の行に外国人の担当者、実施言語が空の項目に支援言語を入れる。"""
    content = template_content()
    staff: Staff | None = worker.assigned_staff
    for code, activity in content.activities.items():
        if activity.planned and not activity.outsourced and activity.staff_id is None and staff:
            content.activities[code] = replace(activity, staff_id=staff.pk)
    for item, item_content in content.items.items():
        if not item_content.language_ids:
            content.items[item] = replace(item_content, language_ids=[worker.support_language_id])
    return content


@transaction.atomic
def create_plan(
    worker: ForeignWorker,
    host: HostOrganization,
    *,
    applies_from: datetime.date,
    by: User | None,
    content: PlanContent | None = None,
) -> SupportPlan:
    """外国人×受入れ機関の計画を作る（中身を省略すると標準の計画から複製）。"""
    if SupportPlan.objects.filter(worker=worker, host_organization=host).exists():
        raise ValidationError(
            {"host_organization": [_("A support plan for this host organization already exists.")]}
        )
    plan = SupportPlan.objects.create(
        worker=worker, host_organization=host, created_by=by, updated_by=by
    )
    add_version(plan, content or content_for_worker(worker), applies_from=applies_from, by=by)
    return plan


# --- 参照・差分 -----------------------------------------------------------------------


def latest_version(plan: SupportPlan) -> SupportPlanVersion | None:
    return plan.versions.order_by("-version").first()


def current_version(plan: SupportPlan, on: datetime.date) -> SupportPlanVersion | None:
    """その日に適用されている版（適用開始日がその日以前の最も新しい版）。"""
    return plan.versions.filter(applies_from__lte=on).order_by("-applies_from", "-version").first()


def snapshot(version: SupportPlanVersion) -> dict[str, Any]:
    """差分の比較に使う値（欄の場所 → 値）。"""
    content = content_of(version)
    values: dict[str, Any] = {
        "plan.created_on": content.created_on,
        "plan.responsible_name": content.responsible_name,
        "plan.translation_language_id": content.translation_language_id,
        "plan.interview_preference": content.interview_preference,
    }
    for item, item_content in content.items.items():
        for name, value in asdict(item_content).items():
            values[f"item:{item}:{name}"] = value
    for code, activity in content.activities.items():
        for name, value in asdict(activity).items():
            values[f"activity:{code}:{name}"] = value
    for name, value in asdict(content.housing).items():
        values[f"housing:{name}"] = value
    for name, value in asdict(content.consultation).items():
        values[f"consultation:{name}"] = value
    for check in content.municipality_checks:
        for name, value in asdict(check).items():
            values[f"municipality:{check.place}:{name}"] = value
    return values


def diff(old: SupportPlanVersion, new: SupportPlanVersion) -> dict[str, list[Any]]:
    """{欄の場所: [変更前, 変更後]}（変わった欄だけ）。"""
    before, after = snapshot(old), snapshot(new)
    return {
        key: [before.get(key), after.get(key)]
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    }
