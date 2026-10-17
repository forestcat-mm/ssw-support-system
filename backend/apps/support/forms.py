"""支援計画の編集画面のフォーム（T17）。項目の型の検査だけを行い、様式に沿った検査は services。

1 画面に 10項目・33 行・住居の概要・相談の受付・共生施策の確認をまとめて出すため、欄ごとのフォームを
`PlanEditor` で束ねる。PlanContent との変換と、サービスのエラー（"activity:2.a:methods" など）を
該当する入力欄に戻すのもここで行う。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.core.models import Staff, SupportLanguage
from apps.core.web_forms import StyledFormMixin
from apps.support.models import (
    CheckMethod,
    ContactMethod,
    HousingTiming,
    InterviewPreference,
    MunicipalityPlace,
    PropertyType,
)
from services import support_plans
from services.support_catalog import (
    ITEMS_WITH_HOURS,
    ITEMS_WITH_LANGUAGE,
    METHODS_WITH_NOTE,
    ROWS,
    SECTIONS,
    Row,
    Section,
    SupportItem,
    labels_of,
)
from services.support_plans import (
    ActivityContent,
    ConsultationContent,
    HousingContent,
    ItemContent,
    MunicipalityContent,
    PlanContent,
)

YES_NO = [("yes", _("yes")), ("no", _("no"))]
DAYS = [
    ("mon", _("Monday")),
    ("tue", _("Tuesday")),
    ("wed", _("Wednesday")),
    ("thu", _("Thursday")),
    ("fri", _("Friday")),
    ("sat", _("Saturday")),
    ("sun", _("Sunday")),
    ("holiday", _("Holidays")),
]


def _prefix(code: str) -> str:
    return code.replace(".", "_")


def active_staff() -> Any:
    return Staff.objects.filter(is_active=True).order_by("name_kana", "name")


class StaffChoiceField(forms.ModelChoiceField):  # type: ignore[type-arg]
    def label_from_instance(self, obj: Any) -> str:
        return f"{obj.name}（{obj.position}）" if obj.position else str(obj.name)


class VersionForm(StyledFormMixin, forms.Form):
    """版と計画全体の欄。標準の計画では適用開始日を出さない（今日）。"""

    applies_from = forms.DateField(label=_("applies from"), widget=forms.DateInput)
    change_reason = forms.CharField(
        label=_("change reason"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text=_("Required from the second version."),
    )
    created_on = forms.DateField(label=_("prepared on"), required=False, widget=forms.DateInput)
    responsible_name = forms.CharField(
        label=_("name of the person responsible for preparation"), required=False, max_length=100
    )
    translation_language = forms.ModelChoiceField(
        label=_("language of the translation given to the worker"),
        queryset=SupportLanguage.objects.all(),
        required=False,
    )
    interview_preference = forms.ChoiceField(
        label=_("worker's preference for periodic interviews"),
        choices=[("", "—"), *InterviewPreference.choices],
        required=False,
    )

    def __init__(self, *args: Any, with_applies_from: bool = True, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if not with_applies_from:
            del self.fields["applies_from"]
        self.style_fields()


class ItemForm(StyledFormMixin, forms.Form):
    languages = forms.ModelMultipleChoiceField(
        label=_("languages"),
        queryset=SupportLanguage.objects.all(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    interpreter = forms.CharField(
        label=_("interpreter (organization and name)"), required=False, max_length=200
    )
    planned_hours = forms.DecimalField(
        label=_("planned hours in total"),
        required=False,
        min_value=0,
        max_digits=5,
        decimal_places=1,
        widget=forms.NumberInput(attrs={"step": "0.5"}),
    )

    def __init__(self, *args: Any, item: SupportItem, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.item = item
        if item not in ITEMS_WITH_HOURS:
            del self.fields["planned_hours"]
        self.style_fields()


class ActivityForm(StyledFormMixin, forms.Form):
    """様式の 1 行（実施予定・担当者・実施方法・委託先）。"""

    planned = forms.ChoiceField(label=_("planned"), choices=YES_NO, widget=forms.RadioSelect)
    planned_note = forms.CharField(
        label=_("timing, or reason for not implementing"), required=False, max_length=200
    )
    content = forms.CharField(
        label=_("support content"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )
    staff = StaffChoiceField(
        label=_("support staff in charge"), queryset=Staff.objects.none(), required=False
    )
    methods = forms.MultipleChoiceField(
        label=_("methods"), required=False, widget=forms.CheckboxSelectMultiple
    )
    method_text = forms.CharField(
        label=_("method (text)"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )
    outsourced = forms.BooleanField(label=_("outsourced to a third party"), required=False)
    outsourcee_name = forms.CharField(label=_("name of the contractor"), required=False)
    outsourcee_title = forms.CharField(label=_("title of the contractor"), required=False)
    outsourcee_address = forms.CharField(label=_("address of the contractor"), required=False)
    note = forms.CharField(
        label=_("notes when different for each content"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    def __init__(self, *args: Any, row: Row, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.row = row
        staff = self.fields["staff"]
        assert isinstance(staff, forms.ModelChoiceField)
        staff.queryset = active_staff()
        if not row.is_free:
            del self.fields["content"]
        methods = self.fields["methods"]
        assert isinstance(methods, forms.MultipleChoiceField)
        if row.methods:
            methods.choices = [(m.value, m.label) for m in row.methods]
            del self.fields["method_text"]
            for method in row.methods:
                if method in METHODS_WITH_NOTE:
                    self.fields[f"note_{method.value}"] = forms.CharField(
                        label=_("%(method)s (details)") % {"method": method.label},
                        required=False,
                        max_length=100,
                    )
        else:
            del self.fields["methods"]
        self.style_fields()

    @property
    def labels(self) -> list[tuple[str, str]]:
        return labels_of(self.row)

    def note_fields(self) -> list[Any]:
        return [self[name] for name in self.fields if name.startswith("note_")]

    def outsourcee_fields(self) -> list[Any]:
        return [
            self[name] for name in ("outsourcee_name", "outsourcee_title", "outsourcee_address")
        ]

    def main_fields(self) -> list[Any]:
        names = ["planned", "planned_note", "content", "staff", "methods", "method_text"]
        return [self[name] for name in names if name in self.fields]


class HousingForm(StyledFormMixin, forms.Form):
    timing = forms.ChoiceField(
        label=_("when secured"), choices=[("", "—"), *HousingTiming.choices], required=False
    )
    room_area_ok = forms.BooleanField(
        label=_("room area: 7.5 m² or more per person"), required=False
    )
    occupants = forms.IntegerField(label=_("number of occupants"), required=False, min_value=1)
    bedroom_area_ok = forms.BooleanField(
        label=_("bedroom area: 4.5 m² or more per person"), required=False
    )
    rent_collected = forms.ChoiceField(
        label=_("housing costs collected"), choices=[("", "—"), *YES_NO], required=False
    )
    monthly_rent = forms.IntegerField(
        label=_("housing costs per month (yen)"), required=False, min_value=0
    )
    property_type = forms.ChoiceField(
        label=_("type of accommodation"), choices=[("", "—"), *PropertyType.choices], required=False
    )
    rent_explanation = forms.CharField(
        label=_("explanation that the cost is appropriate"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()


class ConsultationForm(StyledFormMixin, forms.Form):
    methods = forms.MultipleChoiceField(
        label=_("consultation methods"),
        choices=ContactMethod.choices,
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    phone = forms.CharField(label=_("phone number"), required=False, max_length=30)
    email = forms.EmailField(label=_("email"), required=False)
    other = forms.CharField(label=_("other method"), required=False, max_length=200)
    emergency_methods = forms.MultipleChoiceField(
        label=_("methods in an emergency"),
        choices=ContactMethod.choices,
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    emergency_phone = forms.CharField(
        label=_("phone number in an emergency"), required=False, max_length=30
    )
    emergency_email = forms.EmailField(label=_("email in an emergency"), required=False)
    emergency_other = forms.CharField(
        label=_("other method in an emergency"), required=False, max_length=200
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for day, label in DAYS:
            for edge, edge_label in (("start", _("from")), ("end", _("to"))):
                self.fields[f"{day}_{edge}"] = forms.TimeField(
                    label=f"{label} {edge_label}",
                    required=False,
                    widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
                )
        self.style_fields()

    def hour_rows(self) -> list[tuple[str, Any, Any]]:
        return [(str(label), self[f"{day}_start"], self[f"{day}_end"]) for day, label in DAYS]

    def contact_fields(self) -> list[Any]:
        names = ["methods", "phone", "email", "other"]
        return [self[name] for name in names]

    def emergency_fields(self) -> list[Any]:
        names = ["emergency_methods", "emergency_phone", "emergency_email", "emergency_other"]
        return [self[name] for name in names]


class MunicipalityForm(StyledFormMixin, forms.Form):
    same_as_site = forms.BooleanField(label=_("same as the workplace"), required=False)
    prefecture = forms.CharField(label=_("prefecture"), required=False, max_length=10)
    municipality = forms.CharField(label=_("municipality"), required=False, max_length=50)
    checked_on = forms.DateField(label=_("checked on"), required=False, widget=forms.DateInput)
    method = forms.ChoiceField(
        label=_("how checked"), choices=[("", "—"), *CheckMethod.choices], required=False
    )
    method_note = forms.CharField(label=_("other method (details)"), required=False, max_length=200)
    note = forms.CharField(label=_("remarks"), required=False, max_length=200)

    def __init__(self, *args: Any, place: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.place = place
        if place != MunicipalityPlace.RESIDENCE:
            del self.fields["same_as_site"]
        self.style_fields()

    @property
    def place_label(self) -> str:
        return str(MunicipalityPlace(self.place).label)


@dataclass
class SectionForms:
    section: Section
    item_form: ItemForm | None
    activity_forms: list[ActivityForm]
    warnings: dict[str, list[str]]

    @property
    def has_errors(self) -> bool:
        forms_ = [form for form in [self.item_form, *self.activity_forms] if form is not None]
        return any(form.is_bound and form.errors for form in forms_)

    @property
    def rows(self) -> list[tuple[ActivityForm, list[str]]]:
        return [(f, self.warnings.get(f"activity:{f.row.code}", [])) for f in self.activity_forms]

    @property
    def warning_count(self) -> int:
        return sum(len(messages) for _form, messages in self.rows)

    @property
    def is_open(self) -> bool:
        """編集の画面で開いた状態にする区分（エラーか注意がある）。"""
        return self.has_errors or self.warning_count > 0


def _yes_no(value: bool | None) -> str:
    return "" if value is None else ("yes" if value else "no")


class PlanEditor:
    """支援計画の編集画面のフォームを束ねる。"""

    def __init__(
        self,
        data: Any = None,
        *,
        initial: PlanContent,
        initial_applies_from: datetime.date | None = None,
        with_applies_from: bool = True,
    ) -> None:
        self.version_form = VersionForm(
            data,
            prefix="plan",
            with_applies_from=with_applies_from,
            initial={
                "applies_from": initial_applies_from,
                "created_on": initial.created_on,
                "responsible_name": initial.responsible_name,
                "translation_language": initial.translation_language_id,
                "interview_preference": initial.interview_preference,
            },
        )
        self.item_forms: dict[str, ItemForm] = {}
        for item in SupportItem:
            if item in ITEMS_WITH_LANGUAGE or item in ITEMS_WITH_HOURS:
                content = initial.items.get(item.value, ItemContent())
                self.item_forms[item.value] = ItemForm(
                    data,
                    prefix=f"item-{item.value}",
                    item=item,
                    initial={
                        "languages": content.language_ids,
                        "interpreter": content.interpreter,
                        "planned_hours": content.planned_hours,
                    },
                )
        self.initial = initial
        self.activity_forms: dict[str, ActivityForm] = {}
        for row in ROWS:
            activity = initial.activities.get(row.code, ActivityContent())
            values: dict[str, Any] = {
                "planned": _yes_no(activity.planned),
                "planned_note": activity.planned_note,
                "content": activity.content,
                "staff": activity.staff_id,
                "methods": activity.methods,
                "method_text": activity.method_text,
                "outsourced": activity.outsourced,
                "outsourcee_name": activity.outsourcee_name,
                "outsourcee_title": activity.outsourcee_title,
                "outsourcee_address": activity.outsourcee_address,
                "note": activity.note,
            }
            for method, text in activity.method_notes.items():
                values[f"note_{method}"] = text
            self.activity_forms[row.code] = ActivityForm(
                data, prefix=f"act-{_prefix(row.code)}", row=row, initial=values
            )
        housing = initial.housing
        self.housing_form = HousingForm(
            data,
            prefix="housing",
            initial={
                **{name: getattr(housing, name) for name in HousingContent.__dataclass_fields__},
                "rent_collected": _yes_no(housing.rent_collected),
            },
        )
        consultation = initial.consultation
        hours_initial = {
            f"{day}_{edge}": (consultation.hours.get(day) or ["", ""])[index] or None
            for day, _label in DAYS
            for index, edge in enumerate(("start", "end"))
        }
        self.consultation_form = ConsultationForm(
            data,
            prefix="consultation",
            initial={
                **{
                    name: getattr(consultation, name)
                    for name in ConsultationContent.__dataclass_fields__
                    if name != "hours"
                },
                **hours_initial,
            },
        )
        checks = {check.place: check for check in initial.municipality_checks}
        self.municipality_forms = [
            MunicipalityForm(
                data,
                prefix=f"municipality-{place}",
                place=place,
                initial={
                    name: getattr(checks[place], name)
                    for name in MunicipalityContent.__dataclass_fields__
                    if place in checks and name != "place"
                },
            )
            for place in (MunicipalityPlace.SITE, MunicipalityPlace.RESIDENCE)
        ]

    # --- 束ねた操作 ---------------------------------------------------------------------

    def all_forms(self) -> list[forms.Form]:
        return [
            self.version_form,
            *self.item_forms.values(),
            *self.activity_forms.values(),
            self.housing_form,
            self.consultation_form,
            *self.municipality_forms,
        ]

    def is_valid(self) -> bool:
        return all([form.is_valid() for form in self.all_forms()])

    def warnings(self, *, require_staff: bool = True) -> dict[str, list[str]]:
        """注意（エラーにはしない）。開いたときは初期値から、送信後は入力から判定する。"""
        if not self.version_form.is_bound:
            return support_plans.warnings(self.initial, require_staff=require_staff)
        if not self.is_valid():
            return {}
        return support_plans.warnings(self.to_content(), require_staff=require_staff)

    def sections(self, warnings: dict[str, list[str]]) -> list[SectionForms]:
        return [
            SectionForms(
                section=section,
                item_form=self.item_forms.get(section.item.value) if section.code != "3b" else None,
                activity_forms=[self.activity_forms[row.code] for row in section.rows],
                warnings=warnings,
            )
            for section in SECTIONS
        ]

    def to_content(self) -> PlanContent:
        """入力を PlanContent にする（is_valid() の後に呼ぶ）。画面に無い項目は初期値のまま。"""
        version = self.version_form.cleaned_data
        items = dict(self.initial.items)
        for item in SupportItem:
            item_form = self.item_forms.get(item.value)
            if item_form is None:
                items.setdefault(item.value, ItemContent())
                continue
            item_data = item_form.cleaned_data
            items[item.value] = ItemContent(
                language_ids=sorted(language.pk for language in item_data["languages"]),
                interpreter=item_data["interpreter"],
                planned_hours=item_data.get("planned_hours"),
            )
        activities = {}
        for code, activity_form in self.activity_forms.items():
            data = activity_form.cleaned_data
            staff = data.get("staff")
            methods = list(data.get("methods") or [])
            activities[code] = ActivityContent(
                planned=data["planned"] == "yes",
                planned_note=data["planned_note"].strip(),
                content=data.get("content", "").strip(),
                outsourced=bool(data["outsourced"]),
                staff_id=staff.pk if staff else None,
                outsourcee_name=data["outsourcee_name"].strip(),
                outsourcee_title=data["outsourcee_title"].strip(),
                outsourcee_address=data["outsourcee_address"].strip(),
                methods=methods,
                method_notes={
                    name.removeprefix("note_"): value.strip()
                    for name, value in data.items()
                    if name.startswith("note_") and value.strip()
                },
                method_text=data.get("method_text", "").strip(),
                note=data["note"].strip(),
            )
        housing = self.housing_form.cleaned_data
        consultation = self.consultation_form.cleaned_data
        hours = {}
        for day, _label in DAYS:
            start, end = consultation.get(f"{day}_start"), consultation.get(f"{day}_end")
            if start or end:
                hours[day] = [
                    start.strftime("%H:%M") if start else "",
                    end.strftime("%H:%M") if end else "",
                ]
        checks = []
        for check_form in self.municipality_forms:
            data = check_form.cleaned_data
            values = {name: data.get(name) for name in MunicipalityContent.__dataclass_fields__}
            if any(values[name] for name in values if name not in ("place",)):
                checks.append(
                    MunicipalityContent(
                        place=check_form.place,
                        same_as_site=bool(data.get("same_as_site")),
                        prefecture=data["prefecture"],
                        municipality=data["municipality"],
                        checked_on=data["checked_on"],
                        method=data["method"],
                        method_note=data["method_note"],
                        note=data["note"],
                    )
                )
        language = version.get("translation_language")
        return PlanContent(
            items=items,
            activities=activities,
            housing=HousingContent(
                timing=housing["timing"],
                room_area_ok=bool(housing["room_area_ok"]),
                occupants=housing["occupants"],
                bedroom_area_ok=bool(housing["bedroom_area_ok"]),
                rent_collected=(
                    None if not housing["rent_collected"] else housing["rent_collected"] == "yes"
                ),
                monthly_rent=housing["monthly_rent"],
                property_type=housing["property_type"],
                rent_explanation=housing["rent_explanation"],
            ),
            consultation=ConsultationContent(
                hours=hours,
                methods=list(consultation["methods"]),
                phone=consultation["phone"],
                email=consultation["email"],
                other=consultation["other"],
                emergency_methods=list(consultation["emergency_methods"]),
                emergency_phone=consultation["emergency_phone"],
                emergency_email=consultation["emergency_email"],
                emergency_other=consultation["emergency_other"],
            ),
            municipality_checks=checks,
            created_on=version.get("created_on"),
            responsible_name=version.get("responsible_name", ""),
            translation_language_id=language.pk if language else None,
            interview_preference=version.get("interview_preference", ""),
        )

    def apply_errors(self, error: ValidationError) -> None:
        """サービスのエラー（"activity:<行>:<欄>" など）を、該当する入力欄に付ける。"""
        for key, messages in error.error_dict.items():
            parts = key.split(":")
            form: forms.Form = self.version_form
            field: str | None = None
            if parts[0] == "activity" and parts[1] in self.activity_forms:
                form = self.activity_forms[parts[1]]
                field = {"method_notes": None, "outsourcee_name": "outsourcee_name"}.get(
                    parts[2], parts[2]
                )
            elif parts[0] == "item" and parts[1] in self.item_forms:
                form = self.item_forms[parts[1]]
                field = parts[2]
            elif parts[0] in ("change_reason", "applies_from"):
                field = parts[0]
            for message in messages:
                form.add_error(field if field in form.fields else None, message)
