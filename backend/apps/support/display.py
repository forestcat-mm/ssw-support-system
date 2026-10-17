"""支援計画の表示用の組み立て（T17）。版の中身を様式の区分の順に並べ、名前・選択肢の表示名を付ける。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from apps.core.models import Staff, SupportLanguage
from apps.support.forms import DAYS
from apps.support.models import (
    CheckMethod,
    ContactMethod,
    HousingTiming,
    InterviewPreference,
    MunicipalityPlace,
    PropertyType,
)
from services.support_catalog import (
    ITEMS_WITH_HOURS,
    ITEMS_WITH_LANGUAGE,
    SECTIONS,
    Method,
    Row,
    Section,
    labels_of,
)
from services.support_plans import ActivityContent, ItemContent, PlanContent


def staff_label(staff: Staff | None) -> str:
    if staff is None:
        return ""
    return f"{staff.name}（{staff.position}）" if staff.position else staff.name


@dataclass
class RowView:
    row: Row
    labels: list[tuple[str, str]]
    activity: ActivityContent
    staff: str
    methods: list[str]
    warnings: list[str]


@dataclass
class SectionView:
    section: Section
    item: ItemContent | None
    languages: list[str]
    show_item_fields: bool
    show_hours: bool
    rows: list[RowView] = field(default_factory=list)

    @property
    def planned_count(self) -> int:
        return sum(1 for view in self.rows if view.activity.planned)

    @property
    def staff_names(self) -> list[str]:
        return sorted({view.staff for view in self.rows if view.staff and view.activity.planned})

    @property
    def warning_count(self) -> int:
        return sum(len(view.warnings) for view in self.rows)


@dataclass
class PlanView:
    sections: list[SectionView]
    housing: dict[str, Any]
    consultation: dict[str, Any]
    municipality_checks: list[dict[str, Any]]
    plan_fields: dict[str, Any]

    @property
    def warning_count(self) -> int:
        return sum(section.warning_count for section in self.sections)


def _method_labels(activity: ActivityContent) -> list[str]:
    labels = []
    for code in activity.methods:
        label = str(Method(code).label)
        note = activity.method_notes.get(code)
        labels.append(f"{label}（{note}）" if note else label)
    return labels


def plan_view(content: PlanContent, warnings: dict[str, list[str]]) -> PlanView:
    staff = {member.pk: member for member in Staff.objects.all()}
    languages = {language.pk: str(language) for language in SupportLanguage.objects.all()}
    sections = []
    for section in SECTIONS:
        item = content.items.get(section.item.value)
        show_item = section.code != "3b" and (
            section.item in ITEMS_WITH_LANGUAGE or section.item in ITEMS_WITH_HOURS
        )
        view = SectionView(
            section=section,
            item=item,
            languages=[languages.get(pk, str(pk)) for pk in (item.language_ids if item else [])],
            show_item_fields=show_item,
            show_hours=section.item in ITEMS_WITH_HOURS and section.code != "3b",
        )
        for row in section.rows:
            activity = content.activities.get(row.code, ActivityContent(planned=False))
            view.rows.append(
                RowView(
                    row=row,
                    labels=labels_of(row),
                    activity=activity,
                    staff=staff_label(staff.get(activity.staff_id)) if activity.staff_id else "",
                    methods=_method_labels(activity),
                    warnings=warnings.get(f"activity:{row.code}", []),
                )
            )
        sections.append(view)
    housing = content.housing
    consultation = content.consultation
    return PlanView(
        sections=sections,
        housing={
            "timing": HousingTiming(housing.timing).label if housing.timing else "",
            "room_area_ok": housing.room_area_ok,
            "occupants": housing.occupants,
            "bedroom_area_ok": housing.bedroom_area_ok,
            "rent_collected": housing.rent_collected,
            "monthly_rent": housing.monthly_rent,
            "property_type": PropertyType(housing.property_type).label
            if housing.property_type
            else "",
            "rent_explanation": housing.rent_explanation,
        },
        consultation={
            "hours": [
                (str(label), *consultation.hours[day])
                for day, label in DAYS
                if day in consultation.hours
            ],
            "methods": [ContactMethod(code).label for code in consultation.methods],
            "phone": consultation.phone,
            "email": consultation.email,
            "other": consultation.other,
            "emergency_methods": [
                ContactMethod(code).label for code in consultation.emergency_methods
            ],
            "emergency_phone": consultation.emergency_phone,
            "emergency_email": consultation.emergency_email,
            "emergency_other": consultation.emergency_other,
        },
        municipality_checks=[
            {
                "place": MunicipalityPlace(check.place).label,
                "same_as_site": check.same_as_site,
                "where": f"{check.prefecture} {check.municipality}".strip(),
                "checked_on": check.checked_on,
                "method": CheckMethod(check.method).label if check.method else "",
                "method_note": check.method_note,
                "note": check.note,
            }
            for check in content.municipality_checks
        ],
        plan_fields={
            "created_on": content.created_on,
            "responsible_name": content.responsible_name,
            "translation_language": languages.get(content.translation_language_id or 0, ""),
            "interview_preference": InterviewPreference(content.interview_preference).label
            if content.interview_preference
            else "",
        },
    )
