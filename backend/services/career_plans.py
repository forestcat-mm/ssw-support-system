"""キャリアアッププラン（T36、ADR-0025）。

- 見出しは受入れ機関×分野。事業所ごとに違う場合だけ、事業所の見出しで上書きする（T36-D1）。
- 版は追記だけ。版ごとにレベル（業務・習熟の目安の年数〔文字。T36-D4〕・必要な経験と資格・地位・
  処遇）を持つ。2 版目からは変更理由が必須。
- 雇用契約に使うプランは、契約の事業所の見出し → 受入れ機関×分野の見出しの順に探し、基準日に
  適用される版（無ければ最も古い版）を使う。
- 水産加工業の区分（2027-04-01 から）の契約では、版に「転職できる範囲」が要る（協議会の定め。
  別冊 18頁。T36-D5）。
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils.translation import gettext as _

from apps.contracts.models import EmploymentContract, EmploymentContractVersion
from apps.core.models import User
from apps.hosts.models import (
    CareerPlan,
    CareerPlanLevel,
    CareerPlanVersion,
    HostOrganization,
    HostSite,
    SpecifiedSkillField,
)
from services import contracts

FISHERY_CATEGORY = "fishery_processing"  # 水産加工業の区分（2027-04-01 から）


@dataclass
class LevelInput:
    name: str
    years: str
    duties: str
    requirements: str = ""
    position: str = ""
    treatment: str = ""


@dataclass
class VersionInput:
    applies_from: datetime.date | None
    career_route: str
    transfer_scope: str = ""
    change_reason: str = ""
    levels: Sequence[LevelInput] = field(default_factory=list)


def _errors(
    plan: CareerPlan | None,
    data: VersionInput,
    *,
    host: HostOrganization,
    plan_field: SpecifiedSkillField | None,
    site: HostSite | None,
) -> dict[str, list[str]]:
    errors: dict[str, list[str]] = {}

    def add(key: str, message: str) -> None:
        errors.setdefault(key, []).append(message)

    if plan is None:
        fields = set(host.sites.values_list("field_id", flat=True))
        if plan_field is None or plan_field.pk not in fields:
            add("field", _("Select a field of a site of this host organization."))
        if site is not None and (
            site.host_organization_id != host.pk
            or (plan_field is not None and site.field_id != plan_field.pk)
        ):
            add("site", _("Select a site of this host organization in the field."))
        existing = CareerPlan.objects.filter(host_organization=host, field=plan_field, site=site)
        if plan_field is not None and existing.exists():
            add("field", _("A plan for this field (and site) already exists. Add a new version."))
    else:
        if not data.change_reason.strip():
            add("change_reason", _("Enter the reason for the change."))
        latest = plan.versions.order_by("-version").first()
        if (
            latest is not None
            and data.applies_from is not None
            and data.applies_from < latest.applies_from
        ):
            add(
                "applies_from",
                _("The date must be on or after the date of the previous version (%(day)s).")
                % {"day": latest.applies_from.isoformat()},
            )
    if data.applies_from is None:
        add("applies_from", _("Enter the date."))
    if not data.career_route.strip():
        add("career_route", _("Enter the expected career route."))
    if not data.levels:
        add("levels", _("Enter at least one level."))
    for index, level in enumerate(data.levels):
        if not (level.name.strip() and level.years.strip() and level.duties.strip()):
            add(
                "levels",
                _("Level %(n)d: enter the name, the years and the duties.") % {"n": index + 1},
            )
    return errors


def _raise(errors: dict[str, list[str]]) -> None:
    if errors:
        raise ValidationError(errors)


def _save_version(plan: CareerPlan, data: VersionInput, *, by: User | None) -> CareerPlanVersion:
    number = (plan.versions.aggregate(n=Max("version"))["n"] or 0) + 1
    assert data.applies_from is not None  # 検査済み
    version = CareerPlanVersion.objects.create(
        plan=plan,
        version=number,
        applies_from=data.applies_from,
        change_reason=data.change_reason.strip(),
        career_route=data.career_route.strip(),
        transfer_scope=data.transfer_scope.strip(),
        created_by=by,
        updated_by=by,
    )
    for order, level in enumerate(data.levels, start=1):
        CareerPlanLevel.objects.create(
            version=version,
            order=order,
            name=level.name.strip(),
            years=level.years.strip(),
            duties=level.duties.strip(),
            requirements=level.requirements.strip(),
            position=level.position.strip(),
            treatment=level.treatment.strip(),
            created_by=by,
            updated_by=by,
        )
    return version


@transaction.atomic
def create_plan(
    host: HostOrganization,
    plan_field: SpecifiedSkillField | None,
    site: HostSite | None,
    data: VersionInput,
    *,
    by: User | None,
) -> CareerPlanVersion:
    _raise(_errors(None, data, host=host, plan_field=plan_field, site=site))
    assert plan_field is not None
    plan = CareerPlan.objects.create(
        host_organization=host, field=plan_field, site=site, created_by=by, updated_by=by
    )
    return _save_version(plan, data, by=by)


@transaction.atomic
def add_version(plan: CareerPlan, data: VersionInput, *, by: User | None) -> CareerPlanVersion:
    _raise(_errors(plan, data, host=plan.host_organization, plan_field=plan.field, site=plan.site))
    return _save_version(plan, data, by=by)


def latest_version(plan: CareerPlan) -> CareerPlanVersion | None:
    return plan.versions.order_by("-version").first()


def version_on(plan: CareerPlan, on: datetime.date) -> CareerPlanVersion | None:
    """その日に適用される版（無ければ最も古い版）。"""
    applied = plan.versions.filter(applies_from__lte=on).order_by("-version").first()
    return applied or plan.versions.order_by("version").first()


def plan_for(contract: EmploymentContract, on: datetime.date) -> CareerPlan | None:
    """契約に使うプラン（契約の事業所の見出し → 受入れ機関×分野の見出し）。"""
    version = contracts.version_on(contract, on)
    if version is None:
        return None
    site = version.site
    plans = CareerPlan.objects.filter(
        host_organization=contract.host_organization, field=site.field
    )
    return plans.filter(site=site).first() or plans.filter(site__isnull=True).first()


def plan_version_for(contract: EmploymentContract, on: datetime.date) -> CareerPlanVersion | None:
    plan = plan_for(contract, on)
    return version_on(plan, on) if plan is not None else None


def is_fishery(version: EmploymentContractVersion | None) -> bool:
    """契約の版の業務区分に水産加工業の区分がある（2027-04-01 からの区分）。"""
    return version is not None and version.categories.filter(code=FISHERY_CATEGORY).exists()


def plans_of(host: HostOrganization) -> list[tuple[CareerPlan, CareerPlanVersion | None]]:
    return [
        (plan, latest_version(plan))
        for plan in CareerPlan.objects.filter(host_organization=host).select_related(
            "field", "site"
        )
    ]


def initial_from(version: CareerPlanVersion) -> VersionInput:
    """新しい版の入力の初期値（前の版を写す。変更理由と適用開始日は写さない）。"""
    return VersionInput(
        applies_from=None,
        career_route=version.career_route,
        transfer_scope=version.transfer_scope,
        levels=[
            LevelInput(
                name=level.name,
                years=level.years,
                duties=level.duties,
                requirements=level.requirements,
                position=level.position,
                treatment=level.treatment,
            )
            for level in version.levels.order_by("order", "pk")
        ],
    )
