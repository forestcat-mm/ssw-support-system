"""seed_scenario（手動のテストシナリオの本線。王 麗娜さん）。

- 節 1〜8 の記録が、シナリオの値のとおりに入る。わざと誤る手順の行は作らない。
- --until N で節 N まで。--with-docs が無いときは生成物を作らない。
- 何度実行しても同じ（作成済みなら何もしない。--reset で作り直す）。seed_dummy のダミーデータは
  消さない。seed_dummy の作り直しはシナリオも消す（ダミーの担当者を参照するため）。
"""

from datetime import date
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone
from pytest_django.fixtures import Settings

from apps.contracts.models import ContractWageExplanation
from apps.core.models import Attachment, Role, User, UserRole
from apps.forms_engine.models import GeneratedDocument
from apps.hosts.models import HostOrganization, SupportContractFee
from apps.seed import builders, scenario
from apps.seed.models import SeedObject
from apps.support.models import (
    Briefing,
    Consultation,
    ConsultationStatus,
    DocumentDelivery,
    PeriodicInterview,
    SupportPlan,
    SupportRecord,
)
from apps.workers.models import ForeignWorker, WorkerStatus
from services import deadlines, form_templates, worker_list
from services.worker_list import WorkerListQuery

pytestmark = pytest.mark.django_db

BASE = date(2026, 10, 16)


@pytest.fixture(autouse=True)
def _seeded(settings: Settings) -> None:
    settings.DEBUG = True
    settings.ALLOW_DUMMY_DATA = True
    call_command("seed_dummy", "--date", BASE.isoformat(), stdout=StringIO())


def seed_scenario(*args: str) -> str:
    out = StringIO()
    call_command("seed_scenario", *args, stdout=out)
    return out.getvalue()


def test_the_main_line_until_section_8() -> None:
    dummy_rows = SeedObject.objects.count()
    output = seed_scenario()
    assert "Values not stored" in output and "出生地" in output
    host = HostOrganization.objects.get(name=scenario.HOST_NAME)
    assert host.corporate_number == "7123456789012"
    site = host.sites.get()
    assert (site.industry_code, site.phone_number) == ("0996", "047-400-1235")
    assert host.cooperation_confirmations.count() == 2
    support = host.support_contracts.get()
    assert [(f.name, f.amount) for f in SupportContractFee.objects.filter(contract=support)] == [
        ("支援業務の実施", 25000),
        ("通訳・翻訳", 5000),
    ]
    plan_version = host.career_plans.get().versions.get()
    assert plan_version.levels.count() == 3
    assert plan_version.used_at is not None  # 交付記録で使った（J1）

    worker = ForeignWorker.objects.get(name_romaji=scenario.WORKER_ROMAJI)
    assert worker.name_kana == "ワン　リナ" and worker.status == WorkerStatus.RESIDING
    assert worker.assigned_staff is not None and worker.assigned_staff.user is not None
    version = worker.employment_contracts.get().versions.get()
    assert (version.base_wage, version.hourly_equivalent, version.monthly_working_minutes) == (
        210000,
        1313,
        9600,
    )
    explanation = ContractWageExplanation.objects.get(version=version)
    assert explanation.japanese_wage == 210000 and explanation.japanese_age == 21
    assert SupportPlan.objects.filter(worker=worker).count() == 1

    guidance, orientation = (
        Briefing.objects.filter(worker=worker).order_by("record__occurred_on").all()
    )
    assert guidance.interpreter == "陳 美玲" and guidance.interpreter_kind == "other"
    assert orientation.sessions.count() == 2
    health = Attachment.objects.get(
        content_type__model="foreignworker", object_id=worker.pk, kind="health_check_record"
    )
    assert health.examined_on == date(2026, 6, 25)
    assert DocumentDelivery.objects.filter(worker=worker, kind="career_up_plan").count() == 1

    # 入国後: 在留カードの行が最新で、一覧に在留期限が出る（認定証明書の行の扱いの直し）
    admin = User.objects.create(username="scenario-admin")
    UserRole.objects.create(user=admin, role=Role.objects.get(code="admin"))
    rows = worker_list.list_workers(admin, WorkerListQuery(text="WANG LINA"), BASE).rows
    assert [row.expires_on for row in rows] == [date(2027, 9, 15)]
    assert rows[0].deadline == deadlines.residence_expiry(date(2027, 9, 15))
    kinds = sorted(
        SupportRecord.objects.filter(worker=worker, voided_at__isnull=True)
        .exclude(kind__in=["pre_guidance", "orientation", "periodic_interview", "consultation"])
        .values_list("kind", flat=True)
    )
    assert kinds == [
        "airport_transfer",
        "exchange",
        "housing_and_contracts",
        "housing_and_contracts",
        "japanese_learning",
        "public_procedures",
    ]
    interviews = list(PeriodicInterview.objects.filter(worker=worker).order_by("basis_on"))
    assert [i.basis_on for i in interviews] == [date(2026, 10, 15), date(2026, 10, 17)]
    assert interviews[1].violation_suspected and interviews[1].report_required
    consultation = Consultation.objects.get(worker=worker)
    assert consultation.status == ConsultationStatus.RESOLVED
    assert consultation.entries.count() == 2
    assert not GeneratedDocument.objects.filter(worker=worker).exists()  # --with-docs が無い

    # 何度実行しても同じ。ダミーデータはそのまま
    assert "already exist" in seed_scenario()
    rows_before = SeedObject.scenario_objects.count()
    seed_scenario("--reset")
    assert SeedObject.scenario_objects.count() == rows_before
    assert ForeignWorker.objects.filter(name_romaji=scenario.WORKER_ROMAJI).count() == 1
    assert SeedObject.objects.count() == dummy_rows


def test_until_stops_at_the_section() -> None:
    seed_scenario("--until", "3")
    worker = ForeignWorker.objects.get(name_romaji=scenario.WORKER_ROMAJI)
    assert worker.status == WorkerStatus.PRE_ENTRY
    assert not worker.employment_contracts.exists()
    assert not worker.residence_records.exists()
    contact = worker.contacts.get()
    assert contact.home_country_address.startswith("中国河南省") and contact.effective_from is None


def test_resetting_the_dummy_data_also_removes_the_scenario() -> None:
    seed_scenario("--until", "8")
    builders.reset()
    assert not SeedObject.scenario_objects.exists()
    assert not ForeignWorker.objects.filter(name_romaji=scenario.WORKER_ROMAJI).exists()
    assert not HostOrganization.objects.filter(name=scenario.HOST_NAME).exists()


def test_as_of_moves_now_only_inside() -> None:
    before = timezone.localdate()
    with scenario.as_of(date(2026, 7, 7)):
        assert timezone.localdate() == date(2026, 7, 7)
    assert timezone.localdate() == before


def test_with_docs_generates_and_signs() -> None:
    form_templates.load_all()  # 開発用 DB の make forms にあたる
    seed_scenario("--with-docs")
    worker = ForeignWorker.objects.get(name_romaji=scenario.WORKER_ROMAJI)
    documents = GeneratedDocument.objects.filter(worker=worker)
    forms = sorted(documents.values_list("template__form_id", flat=True))
    assert forms == sorted(
        [
            "career-plan",
            "1-17",
            "1-6",
            "1-5",
            "1-4",
            "13-1",
            "13-2",
            "1-25",
            "5-10",
            "5-9",
            "5-8",
            "5-5",
            "5-6",
            "5-5",
            "5-6",
        ]
    ), forms
    assert all(document.status == "completed" for document in documents), [
        d.failure_reason for d in documents
    ]
    signed = documents.filter(signatures__isnull=False).distinct()
    assert sorted(signed.values_list("template__form_id", flat=True)) == sorted(
        ["1-17", "1-5", "1-6", "5-9", "career-plan", "5-8"]
    )
    host = HostOrganization.objects.get(name=scenario.HOST_NAME)
    assert GeneratedDocument.objects.filter(object_id=host.pk, template__form_id="5-4").exists()


def test_records_made_on_the_screens_stop_the_seed() -> None:
    """画面で作った同じ受入れ機関（台帳に無い）は消さずに、作るのを止める。"""
    HostOrganization.objects.create(
        name=scenario.HOST_NAME,
        corporate_number="7123456789012",
        representative_name="高橋 誠",
        postal_code="273-0012",
        address="千葉県船橋市浜町四丁目10番1号",
    )
    with pytest.raises(CommandError, match="made on the screens"):
        seed_scenario()
    assert HostOrganization.objects.filter(name=scenario.HOST_NAME).count() == 1
    assert not SeedObject.scenario_objects.exists()
