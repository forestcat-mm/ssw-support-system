"""キャリアアッププラン・実務経験証明書（自機関のテンプレート。T36、ADR-0025）。

- プランは受入れ機関×分野（事業所の上書き）・版（追記だけ）・レベル。
- テンプレートは道具の出力そのもの（空の様式 original.docx と template.docx を同じ定義から作る）。
- 生成の対象は雇用契約。使ったプランの版をスナップショットに残す。
- 生成物から交付記録を作れる。水産加工業の区分の契約は「同意を得た」の印が無いと注意。
"""

import datetime
import importlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.core.exceptions import ValidationError
from pytest_django.fixtures import Settings as SettingsWrapper

from apps.core.models import AuditAction, AuditLog
from apps.forms_engine.models import GeneratedDocument, GenerationStatus
from apps.hosts.models import CareerPlanImmutableError, CareerPlanVersion
from apps.support.models import DeliveryKind
from services import (
    career_plans,
    deliveries,
    form_data_career,
    form_docx,
    form_generation,
    form_templates,
)
from services.deliveries import DeliveryInput
from tests.contracts.factories import make_contract
from tests.core.factories import make_user, verified_client
from tests.forms_engine import pdfcheck
from tests.forms_engine.test_form_contract import body_texts
from tests.hosts.factories import category, make_host, make_site
from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db

ROOT = Path(settings.FORM_TEMPLATES_ROOT)
MAPPINGS = Path(settings.FORM_MAPPINGS_ROOT)
TOOLS = ROOT.parent.parent / "tools" / "forms"
VALID_FROM = "2026-10-18"
BASIS = datetime.date(2026, 10, 18)


def levels(count: int = 2) -> list[career_plans.LevelInput]:
    return [
        career_plans.LevelInput(
            name=f"レベル{n}",
            years_from=n,
            years_to=n,
            duties=f"業務{n}",
            requirements=f"資格{n}",
            position=f"地位{n}",
            treatment=f"処遇{n}",
        )
        for n in range(1, count + 1)
    ]


def plan_input(**overrides: Any) -> career_plans.VersionInput:
    values: dict[str, Any] = {
        "applies_from": datetime.date(2026, 1, 1),
        "career_route": "作業員 → ライン長",
        "levels": levels(),
    }
    return career_plans.VersionInput(**(values | overrides))


@pytest.fixture
def food() -> Any:
    """食品の事業所の受入れ機関と、その契約（飲食料品製造業全般）。"""
    host = make_host()
    site = make_site(host, "food_manufacturing", name="食品工場")
    site.categories.set([category("food_manufacturing_general")])
    contract = make_contract(make_worker(), site=site)
    version = contract.versions.get()
    version.categories.set([category("food_manufacturing_general")])
    return contract


# --- テンプレート ------------------------------------------------------------------------------


@pytest.mark.parametrize("form_id", ["career-plan", "experience-certificate"])
def test_own_templates_are_reproduced_by_the_tool(tmp_path: Path, form_id: str) -> None:
    result = subprocess.run(
        [sys.executable, str(TOOLS / "make_own_templates.py"), form_id, str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    for name in ("original.docx", "template.docx"):
        assert (tmp_path / name).read_bytes() == (ROOT / form_id / VALID_FROM / name).read_bytes()
    assert result.stdout == (MAPPINGS / form_id / f"{VALID_FROM}.json").read_text("utf-8")


@pytest.mark.parametrize("form_id", ["career-plan", "experience-certificate"])
def test_template_rendered_empty_is_the_blank_form(tmp_path: Path, form_id: str) -> None:
    out = tmp_path / "empty.docx"
    form_docx.render(ROOT / form_id / VALID_FROM / "template.docx", out, {})
    assert body_texts(out) == body_texts(ROOT / form_id / VALID_FROM / "original.docx")


def test_self_made_manifest_needs_the_tool_and_no_url(
    repo_copy: tuple[Path, Path], settings: SettingsWrapper
) -> None:
    templates, _mappings = repo_copy
    path = templates / "career-plan" / VALID_FROM / "manifest.json"
    data = json.loads(path.read_text("utf-8"))
    data["source"]["url"] = "https://example.com/plan.docx"
    path.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    with pytest.raises(form_templates.FormTemplateError):
        form_templates.load_all()
    data["source"]["url"] = ""
    data["source"]["self_made"] = False
    path.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    with pytest.raises(form_templates.FormTemplateError):  # 自作でなければ URL が要る
        form_templates.load_all()


# --- プラン -----------------------------------------------------------------------------------


def test_plan_versions_and_levels_are_kept(food: Any) -> None:
    host = food.host_organization
    field = food.versions.get().site.field
    first = career_plans.create_plan(host, field, None, plan_input(), by=None)
    assert [level.name for level in first.levels.order_by("order")] == ["レベル1", "レベル2"]
    with pytest.raises(ValidationError) as error:
        career_plans.add_version(first.plan, plan_input(), by=None)  # 変更理由が要る
    assert "change_reason" in error.value.message_dict
    second = career_plans.add_version(
        first.plan,
        plan_input(applies_from=datetime.date(2026, 9, 1), change_reason="処遇の見直し"),
        by=None,
    )
    assert second.version == 2
    assert career_plans.version_on(first.plan, datetime.date(2026, 8, 31)) == first
    assert career_plans.version_on(first.plan, BASIS) == second
    first.career_route = "直す"
    first.save()  # 使う前は直せる（修正 1 回目 J1）
    career_plans.mark_used(first)
    first.career_route = "もう一度直す"
    with pytest.raises(CareerPlanImmutableError):
        first.save()
    with pytest.raises(CareerPlanImmutableError):
        first.levels.first().delete()  # type: ignore[union-attr]
    assert (
        AuditLog.objects.filter(
            action=AuditAction.CREATE, content_type__model="careerplanversion"
        ).count()
        == 2
    )


def test_plan_checks(food: Any) -> None:
    host = food.host_organization
    site = food.versions.get().site
    industrial = make_site(host, "industrial_products", name="工場")
    career_plans.create_plan(host, site.field, None, plan_input(), by=None)
    cases = [
        (site.field, None, plan_input(), "field"),  # 同じ見出しがある
        (industrial.field, site, plan_input(), "site"),  # 分野の違う事業所
        (site.field, site, plan_input(levels=[]), "levels"),
        (site.field, site, plan_input(career_route=""), "career_route"),
    ]
    for plan_field, plan_site, data, key in cases:
        with pytest.raises(ValidationError) as error:
            career_plans.create_plan(host, plan_field, plan_site, data, by=None)
        assert key in error.value.message_dict


def test_site_plan_overrides_the_field_plan(food: Any) -> None:
    host = food.host_organization
    site = food.versions.get().site
    general = career_plans.create_plan(host, site.field, None, plan_input(), by=None)
    assert career_plans.plan_for(food, BASIS) == general.plan
    own = career_plans.create_plan(host, site.field, site, plan_input(), by=None)
    assert career_plans.plan_for(food, BASIS) == own.plan


def test_unused_versions_can_be_edited_until_used(loaded: None, food: Any) -> None:
    """生成にも交付にも使っていない版は直せる。生成・交付で使った日時が入ると固定（J1）。"""
    host = food.host_organization
    first = career_plans.create_plan(
        host, food.versions.get().site.field, None, plan_input(), by=None
    )
    edited = career_plans.edit_version(
        first,
        plan_input(career_route="作業員 → 班長", levels=levels(3)),
        by=None,
    )
    assert edited.version == 1 and edited.career_route == "作業員 → 班長"
    assert edited.levels.count() == 3
    assert AuditLog.objects.filter(
        action=AuditAction.UPDATE, content_type__model="careerplanversion"
    ).exists()
    assert (
        AuditLog.objects.filter(
            action=AuditAction.DELETE, content_type__model="careerplanlevel"
        ).count()
        == 2
    )
    second = career_plans.add_version(
        first.plan,
        plan_input(applies_from=datetime.date(2026, 9, 1), change_reason="処遇の見直し"),
        by=None,
    )
    with pytest.raises(ValidationError) as error:  # 次の版の適用開始日より後にはできない
        career_plans.edit_version(
            first, plan_input(applies_from=datetime.date(2026, 10, 1)), by=None
        )
    assert "applies_from" in error.value.message_dict
    with pytest.raises(ValidationError) as error:  # 2 版目以降は変更理由が要る
        career_plans.edit_version(second, plan_input(applies_from=BASIS), by=None)
    assert "change_reason" in error.value.message_dict
    document = generate(food, "career-plan")  # 基準日に適用されるのは第 2 版
    assert json.loads(document.snapshot)["data"]["p.plan_version_id"] == second.pk
    second.refresh_from_db()
    first.refresh_from_db()
    assert second.used_at is not None and first.used_at is None
    with pytest.raises(ValidationError):
        career_plans.edit_version(
            second, plan_input(applies_from=BASIS, change_reason="直す"), by=None
        )
    # 交付記録で使った版も固定する（交付日に適用されていた版）
    deliveries.create_delivery(
        food.worker,
        DeliveryInput(
            kind=DeliveryKind.CAREER_UP_PLAN,
            delivered_on=datetime.date(2026, 3, 1),
            method="hand",
            delivered_by="organization",
            explained=True,
            contract=food,
        ),
        by=None,
        today=BASIS,
    )
    first.refresh_from_db()
    assert first.used_at is not None


def test_years_are_two_numbers(food: Any) -> None:
    """習熟の目安の年数は「◯年目から◯年目まで」（終わりが空なら「◯年目以降」。J6）。"""
    level = career_plans.LevelInput
    assert career_plans.years_text(level("L", 1, 2, "d")) == "1〜2年目"
    assert career_plans.years_text(level("L", 5, None, "d")) == "5年目以降"
    assert career_plans.years_text(level("L", 3, 3, "d")) == "3年目"
    assert career_plans.years_text(level("L", None, None, "d", years_legacy="入社後すぐ")) == (
        "入社後すぐ"
    )
    host = food.host_organization
    with pytest.raises(ValidationError) as error:
        career_plans.create_plan(
            host,
            food.versions.get().site.field,
            None,
            plan_input(levels=[level("L", 3, 2, "d")]),
            by=None,
        )
    assert "levels" in error.value.message_dict


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1〜2年目", (1, 2)),
        ("1～2年目", (1, 2)),
        ("１－２年目", (1, 2)),
        ("1年目から2年目まで", (1, 2)),
        ("1年目〜2年目", (1, 2)),
        ("5年目以降", (5, None)),
        ("5年目～", (5, None)),
        ("3年目", (3, 3)),
        ("2〜1年目", None),
        ("入社後すぐ", None),
        ("", None),
    ],
)
def test_migration_reads_the_old_years_text(
    text: str, expected: tuple[int, int | None] | None
) -> None:
    migration = importlib.import_module("apps.hosts.migrations.0009_career_plan_drafts_and_years")
    assert migration.parse_years(text) == expected


def test_old_years_text_is_warned_before_generation(loaded: None, food: Any) -> None:
    version = career_plans.create_plan(
        food.host_organization, food.versions.get().site.field, None, plan_input(), by=None
    )
    level = version.levels.order_by("order").first()
    assert level is not None
    level.years_from, level.years_to, level.years_legacy = None, None, "入社後すぐ"
    level.save()  # 移行で読み取れなかった行（使う前の版なので直せる）
    data = form_data_career.build_career_plan(food, BASIS)
    assert data["p.l1_years"] == "入社後すぐ"
    result = form_generation.check("career-plan", target=food, data=data, basis_on=BASIS)
    assert "career_plan_years_text" in [f.code for f in result.warnings]


# --- 生成 ------------------------------------------------------------------------------------


def generate(contract: Any, form_id: str, **kwargs: Any) -> GeneratedDocument:
    data = (
        form_data_career.build_career_plan(contract, BASIS)
        if form_id == "career-plan"
        else form_data_career.build_experience_certificate(contract, BASIS)
    )
    document = form_generation.request(
        form_id, target=contract, data=data, basis_on=BASIS, by=None, **kwargs
    )
    document.refresh_from_db()
    return document


def test_career_plan_needs_a_plan_and_keeps_its_version(loaded: None, food: Any) -> None:
    data = form_data_career.build_career_plan(food, BASIS)
    result = form_generation.check("career-plan", target=food, data=data, basis_on=BASIS)
    assert [f.code for f in result.blocking] == ["career_plan_missing_content"]
    version = career_plans.create_plan(
        food.host_organization, food.versions.get().site.field, None, plan_input(), by=None
    )
    document = generate(food, "career-plan")
    assert document.status == GenerationStatus.COMPLETED, document.failure_reason
    assert document.template.requires_signed_copy is True  # T36-D6
    snapshot = json.loads(document.snapshot)
    assert snapshot["data"]["p.plan_version_id"] == version.pk
    blank = pdfcheck.blank_from_template(ROOT / "career-plan" / VALID_FROM / "template.docx")
    assert document.page_count == len(pdfcheck.pdf_text(blank)) == 1
    text = "\n".join(body_texts(Path(document.filled_file.path)))
    assert "作業員 → ライン長" in text and "レベル2" in text and food.worker.name_romaji in text


def test_levels_beyond_the_rows_are_warned(loaded: None, food: Any) -> None:
    career_plans.create_plan(
        food.host_organization,
        food.versions.get().site.field,
        None,
        plan_input(levels=levels(6)),
        by=None,
    )
    data = form_data_career.build_career_plan(food, BASIS)
    result = form_generation.check("career-plan", target=food, data=data, basis_on=BASIS)
    assert [f.code for f in result.warnings] == ["career_plan_levels_overflow"]


def test_fishery_contract_needs_the_scope_of_job_changes(loaded: None) -> None:
    """水産加工業の区分（2027-04-01 から）の契約は、プランに転職できる範囲が要る（T36-D5）。"""
    host = make_host()
    site = make_site(host, "food_manufacturing", name="水産加工工場")
    site.categories.set([category("fishery_processing")])
    contract = make_contract(
        make_worker(),
        site=site,
        starts_on=datetime.date(2027, 4, 1),
        changed_on=datetime.date(2027, 4, 1),
        ends_on=datetime.date(2028, 3, 31),
    )
    contract.versions.get().categories.set([category("fishery_processing")])
    career_plans.create_plan(host, site.field, None, plan_input(), by=None)
    on = datetime.date(2027, 4, 1)
    data = form_data_career.build_career_plan(contract, on)
    result = form_generation.check("career-plan", target=contract, data=data, basis_on=on)
    assert [f.code for f in result.warnings] == ["transfer_scope_missing"]
    # 交付記録: 同意の印が無いと注意
    delivery = deliveries.create_delivery(
        contract.worker,
        DeliveryInput(
            kind=DeliveryKind.CAREER_UP_PLAN,
            delivered_on=datetime.date(2026, 10, 1),
            method="hand",
            delivered_by="organization",
            explained=True,
            contract=contract,
        ),
        by=None,
        today=BASIS,
    )
    codes = {warning.code for warning in deliveries.warnings(delivery)}
    assert "no_consent" in codes


def test_experience_certificate_period(loaded: None, food: Any) -> None:
    data = form_data_career.build_experience_certificate(food, BASIS)
    assert data["e.period"] == "2026年4月1日から2026年10月18日まで"  # 契約の開始日〜基準日
    document = generate(food, "experience-certificate")
    assert document.status == GenerationStatus.COMPLETED, document.failure_reason
    assert document.template.requires_signed_copy is False  # T36-D7
    early = datetime.date(2026, 3, 1)
    result = form_generation.check(
        "experience-certificate",
        target=food,
        data=form_data_career.build_experience_certificate(food, early),
        basis_on=early,
    )
    assert [f.code for f in result.blocking] == ["experience_not_started"]


def test_delivery_from_the_generated_plan(loaded: None, food: Any) -> None:
    career_plans.create_plan(
        food.host_organization, food.versions.get().site.field, None, plan_input(), by=None
    )
    document = generate(food, "career-plan")
    data = DeliveryInput(
        kind=DeliveryKind.CAREER_UP_PLAN,
        delivered_on=datetime.date(2026, 3, 1),
        method="hand",
        delivered_by="organization",
        explained=True,
        contract=food,
        generated_document=document,
    )
    delivery = deliveries.create_delivery(food.worker, data, by=None, today=BASIS)
    assert delivery.generated_document == document
    other = make_contract(
        make_worker(),
        site=food.versions.get().site,
        starts_on=datetime.date(2028, 4, 1),
        changed_on=datetime.date(2028, 4, 1),
        ends_on=None,
    )
    with pytest.raises(ValidationError):  # 生成物の契約と違う契約
        deliveries.create_delivery(
            other.worker,
            DeliveryInput(**{**data.__dict__, "contract": other}),
            by=None,
            today=BASIS,
        )
    with pytest.raises(ValidationError):  # 同意の印はキャリアアッププランだけ
        deliveries.create_delivery(
            food.worker,
            DeliveryInput(
                kind=DeliveryKind.EXPERIENCE_CERTIFICATE,
                delivered_on=BASIS,
                method="hand",
                delivered_by="organization",
                contract=food,
                consented=True,
            ),
            by=None,
            today=BASIS,
        )


# --- 画面 -----------------------------------------------------------------------------------


def test_plan_screens_and_generation_from_the_contract_tab(loaded: None, food: Any) -> None:
    host = food.host_organization
    client = verified_client(make_user("tanto36", roles=("support_staff",)))
    detail = client.get(f"/hosts/{host.pk}/").content.decode()
    assert 'id="career-plans"' in detail and 'id="career-plan-create"' in detail
    url = f"/hosts/{host.pk}/career-plans/new/"
    page = client.get(url)
    field = food.versions.get().site.field
    post = {
        page.context["once_field"]: page.context["once_token"],
        "field": str(field.pk),
        "applies_from": "2026-01-01",
        "career_route": "作業員 → ライン長",
        "levels-TOTAL_FORMS": "1",
        "levels-INITIAL_FORMS": "0",
        "levels-0-name": "レベル1",
        "levels-0-years_from": "1",
        "levels-0-years_to": "2",
        "levels-0-duties": "製造ラインの作業",
    }
    response = client.post(url, post)
    version = CareerPlanVersion.objects.get()
    assert response["Location"] == f"/hosts/{host.pk}/career-plans/{version.plan_id}/?version=1"
    shown = client.get(response["Location"]).content.decode()
    assert "製造ラインの作業" in shown and 'id="career-plan-new-version"' in shown
    new_version = client.get(f"/hosts/{host.pk}/career-plans/{version.plan_id}/versions/new/")
    assert new_version.context["levels"].initial[0]["name"] == "レベル1"  # 前の版を写す
    # 契約タブから生成
    tab = client.get(f"/workers/{food.worker_id}/contracts/").content.decode()
    assert f"/contracts/{food.pk}/documents/career-plan/" in tab
    generate_url = f"/workers/{food.worker_id}/contracts/{food.pk}/documents/career-plan/"
    page = client.get(generate_url)
    assert "不足はありません" in page.content.decode()
    response = client.post(
        generate_url,
        {
            page.context["once_field"]: page.context["once_token"],
            "basis_on": BASIS.isoformat(),
            "action": "generate",
        },
    )
    document = GeneratedDocument.objects.get(template__form_id="career-plan")
    assert document.target == food
    content = client.get(response["Location"]).content.decode()
    assert (
        f"/support/deliveries/new/{DeliveryKind.CAREER_UP_PLAN}/?generated={document.pk}" in content
    )
    form = client.get(
        f"/workers/{food.worker_id}/support/deliveries/new/{DeliveryKind.CAREER_UP_PLAN}/?generated={document.pk}"
    )
    assert form.context["form"].initial["contract"] == food.pk
    assert "consented" in form.context["form"].fields


def test_draft_version_screen(loaded: None, food: Any) -> None:
    """使っていない版は詳細に「下書き」と「編集」が出て、直せる。使った後は 403（J1）。"""
    version = career_plans.create_plan(
        food.host_organization, food.versions.get().site.field, None, plan_input(), by=None
    )
    host = food.host_organization
    client = verified_client(make_user("tanto36", roles=("support_staff",)))
    detail = client.get(f"/hosts/{host.pk}/career-plans/{version.plan_id}/").content.decode()
    assert 'data-state="draft"' in detail and 'id="career-plan-edit"' in detail
    url = f"/hosts/{host.pk}/career-plans/{version.plan_id}/versions/1/edit/"
    page = client.get(url)
    assert page.status_code == 200
    assert "change_reason" not in page.context["form"].fields  # 第 1 版
    assert page.context["levels"].initial[0]["years_from"] == 1
    post = {
        page.context["once_field"]: page.context["once_token"],
        "applies_from": "2026-01-01",
        "career_route": "作業員 → 班長 → ライン長",
        "levels-TOTAL_FORMS": "1",
        "levels-INITIAL_FORMS": "0",
        "levels-0-name": "レベル1",
        "levels-0-years_from": "1",
        "levels-0-years_to": "",
        "levels-0-duties": "製造ラインの作業",
    }
    response = client.post(url, post)
    assert response.status_code == 302
    version.refresh_from_db()
    assert version.career_route == "作業員 → 班長 → ライン長"
    assert career_plans.years_text(version.levels.get()) == "1年目以降"
    generate(food, "career-plan")
    detail = client.get(f"/hosts/{host.pk}/career-plans/{version.plan_id}/").content.decode()
    assert 'data-state="used"' in detail and 'id="career-plan-edit"' not in detail
    assert client.get(url).status_code == 403
