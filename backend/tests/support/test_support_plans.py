"""支援計画（T16）: 細目の一覧・標準の計画・計画の作成・版の検査と差分・変更の拒否・監査・権限。"""

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from apps.core.models import AuditAction, AuditLog, RolePermission, SupportLanguage
from apps.support.models import ImmutableRowError, SupportPlan
from services import support_plans
from services.support_catalog import (
    ACTIVITY_LABELS,
    ITEMS_WITH_HOURS,
    ROWS,
    Method,
    SupportItem,
    labels_of,
    rows_of,
)
from tests.contracts.factories import site_with_categories
from tests.core.factories import make_staff
from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db


# --- 細目の一覧（参考様式第1-17号） ------------------------------------------------------


def test_catalog_covers_the_form() -> None:
    assert len(ACTIVITY_LABELS) == 42
    for row in ROWS:
        assert len(labels_of(row)) == len(row.letters)  # 細目の文言がそろっている
    # 公的手続への同行は、様式では生活オリエンテーションの b に含まれる（専用の行は無い）
    assert rows_of(SupportItem.PUBLIC_PROCEDURES) == []
    assert "4.main" in {row.code for row in rows_of(SupportItem.ORIENTATION)}
    others = set(SupportItem) - {SupportItem.PUBLIC_PROCEDURES}
    assert all(rows_of(item) for item in others)
    assert ITEMS_WITH_HOURS == {SupportItem.PRE_GUIDANCE, SupportItem.ORIENTATION}


# --- 標準の計画・計画の作成 ---------------------------------------------------------------


def test_template_is_created_once_with_defaults() -> None:
    plan = support_plans.template_plan()
    assert support_plans.template_plan() == plan
    content = support_plans.template_content()
    assert content.activities["1.main"].planned
    assert not content.activities["1.free"].planned
    assert content.activities["9.main"].planned_note == "3か月に1回以上"


def test_create_plan_copies_template_and_fills_staff_and_language() -> None:
    staff = make_staff(name="佐藤")
    worker = make_worker(assigned_staff=staff)
    host = site_with_categories().host_organization
    plan = support_plans.create_plan(worker, host, applies_from=date(2026, 10, 1), by=None)
    version = support_plans.latest_version(plan)
    assert version is not None and version.version == 1
    content = support_plans.content_of(version)
    assert content.activities["1.main"].staff_id == staff.pk
    assert content.activities["1.free"].staff_id is None  # 「無」の行には入れない
    assert content.items[SupportItem.PRE_GUIDANCE].language_ids == [worker.support_language_id]
    assert version.items.count() == 10 and version.activities.count() == len(ROWS)
    with pytest.raises(ValidationError):
        support_plans.create_plan(worker, host, applies_from=date(2026, 10, 1), by=None)


def test_update_template_keeps_history() -> None:
    content = support_plans.template_content()
    content.activities["6.main"] = replace(content.activities["6.main"], planned_note="随時")
    support_plans.update_template(content, change_reason="相談の受付時期を変更", by=None)
    plan = support_plans.template_plan()
    assert [v.version for v in plan.versions.order_by("version")] == [1, 2]
    assert support_plans.template_content().activities["6.main"].planned_note == "随時"


# --- 版・差分 -----------------------------------------------------------------------------


@pytest.fixture
def plan() -> SupportPlan:
    worker = make_worker()
    host = site_with_categories().host_organization
    return support_plans.create_plan(worker, host, applies_from=date(2026, 4, 1), by=None)


def test_new_version_needs_reason_and_later_date(plan: SupportPlan) -> None:
    v1 = support_plans.latest_version(plan)
    assert v1 is not None
    content = support_plans.content_of(v1)
    with pytest.raises(ValidationError) as error:
        support_plans.add_version(plan, content, applies_from=date(2026, 5, 1), by=None)
    assert "change_reason" in error.value.error_dict
    with pytest.raises(ValidationError) as error:
        support_plans.add_version(
            plan, content, applies_from=date(2026, 3, 1), change_reason="変更", by=None
        )
    assert "applies_from" in error.value.error_dict


def test_diff_and_current_version(plan: SupportPlan) -> None:
    v1 = support_plans.latest_version(plan)
    assert v1 is not None
    content = support_plans.content_of(v1)
    content.activities["2.a"] = replace(
        content.activities["2.a"],
        methods=[Method.ARRIVAL_AIRPORT],
        method_notes={Method.ARRIVAL_AIRPORT: "成田"},
    )
    content.items[SupportItem.ORIENTATION] = replace(
        content.items[SupportItem.ORIENTATION], planned_hours=Decimal("8.0")
    )
    v2 = support_plans.add_version(
        plan, content, applies_from=date(2026, 7, 1), change_reason="送迎の空港を追記", by=None
    )
    changes = support_plans.diff(v1, v2)
    assert changes["activity:2.a:methods"] == [[], ["arrival_airport"]]
    assert changes["item:orientation:planned_hours"] == [None, Decimal("8.0")]
    assert "activity:1.main:planned" not in changes
    assert support_plans.current_version(plan, date(2026, 6, 30)) == v1
    assert support_plans.current_version(plan, date(2026, 7, 1)) == v2


@pytest.mark.parametrize(
    ("code", "changes", "key"),
    [
        ("2.a", {"planned": False, "planned_note": ""}, "activity:2.a:planned_note"),
        ("2.a", {"outsourced": True}, "activity:2.a:outsourcee_name"),
        ("2.a", {"methods": [Method.IN_PERSON]}, "activity:2.a:methods"),
        ("1.main", {"methods": [Method.OTHER]}, "activity:1.main:method_notes"),
        ("1.free", {"planned": True, "content": ""}, "activity:1.free:content"),
    ],
)
def test_validation_follows_the_form_notes(
    plan: SupportPlan, code: str, changes: dict[str, object], key: str
) -> None:
    v1 = support_plans.latest_version(plan)
    assert v1 is not None
    content = support_plans.content_of(v1)
    content.activities[code] = replace(content.activities[code], **changes)  # type: ignore[arg-type]
    with pytest.raises(ValidationError) as error:
        support_plans.validate(content)
    assert key in error.value.error_dict


def test_planned_hours_only_for_guidance_and_orientation() -> None:
    content = support_plans.default_content()
    content.items[SupportItem.EXCHANGE] = replace(
        content.items[SupportItem.EXCHANGE], planned_hours=Decimal("1")
    )
    with pytest.raises(ValidationError) as error:
        support_plans.validate(content)
    assert "item:exchange:planned_hours" in error.value.error_dict


def test_versions_and_contents_cannot_be_changed_or_deleted(plan: SupportPlan) -> None:
    version = support_plans.latest_version(plan)
    assert version is not None
    version.change_reason = "書き換え"
    with pytest.raises(ImmutableRowError):
        version.save()
    activity = version.activities.first()
    assert activity is not None
    with pytest.raises(ImmutableRowError):
        activity.delete()


def test_only_one_template(plan: SupportPlan) -> None:
    support_plans.template_plan()
    with pytest.raises(IntegrityError):
        SupportPlan.objects.create(is_template=True)


def test_version_creation_is_audited(plan: SupportPlan) -> None:
    version = support_plans.latest_version(plan)
    assert version is not None
    assert AuditLog.objects.filter(
        action=AuditAction.CREATE,
        content_type__model="supportplanversion",
        object_id=str(version.pk),
    ).exists()
    assert AuditLog.objects.filter(
        action=AuditAction.CREATE, content_type__model="supportplanactivity"
    ).count() >= len(ROWS)


def test_support_permissions_for_both_roles() -> None:
    for role in ("admin", "support_staff"):
        actions = set(
            RolePermission.objects.filter(role__code=role).values_list("action", flat=True)
        )
        assert {"support.view", "support.manage"} <= actions


def test_languages_are_stored_per_item(plan: SupportPlan) -> None:
    v1 = support_plans.latest_version(plan)
    assert v1 is not None
    content = support_plans.content_of(v1)
    both = sorted(SupportLanguage.objects.values_list("pk", flat=True))
    content.items[SupportItem.CONSULTATION] = replace(
        content.items[SupportItem.CONSULTATION], language_ids=both, interpreter="通訳会社 王"
    )
    v2 = support_plans.add_version(
        plan, content, applies_from=date(2026, 4, 2), change_reason="言語を追加", by=None
    )
    stored = support_plans.content_of(v2).items[SupportItem.CONSULTATION]
    assert stored.language_ids == both and stored.interpreter == "通訳会社 王"
