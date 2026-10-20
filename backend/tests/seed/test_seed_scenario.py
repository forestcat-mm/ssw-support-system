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
from apps.core.models import Attachment, AuditAction, AuditLog, Role, User, UserRole
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
    assert "今の作りに欄が無い" in output and "出生地" in output
    assert "テストシナリオの記録を作りました" in output
    assert "入力が必須なので決めて入れたもの" not in output  # シナリオに値が入った（2026-10-20）
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
    assert [(d.kind, d.amount) for d in version.deductions.all()] == [
        ("tax", 4500),
        ("social_insurance", 31900),
        ("employment_insurance", 1300),
        ("housing", 25000),
        ("utilities", 8000),
    ]
    assert (version.hiring_health_check_on, version.first_periodic_health_check_on) == (
        date(2026, 9, 1),
        date(2027, 9, 1),
    )
    assert version.periodic_health_check_interval == "1年"
    explanation = ContractWageExplanation.objects.get(version=version)
    assert explanation.japanese_wage == 210000 and explanation.japanese_age == 21
    assert explanation.rules_wage == 205000
    assert SupportPlan.objects.filter(worker=worker).count() == 1

    guidance, orientation = (
        Briefing.objects.filter(worker=worker).order_by("record__occurred_on").all()
    )
    assert guidance.interpreter == "陳 美玲" and guidance.interpreter_kind == "other"
    assert guidance.foreign_fees == "paid" and guidance.fees_agreed is True
    assert [(f.organization_name, f.paid_on, f.amount_jpy) for f in guidance.fees.all()] == [
        ("鄭州サンプル人材服務（架空の送出機関）", date(2026, 5, 20), 150000)
    ]
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
    # 生成日はその場面の日付（seed の中だけ「今」をずらす）。監査ログの日時は実際の時刻
    plan = documents.get(template__form_id="1-17")
    assert plan.generated_at is not None
    assert timezone.localdate(plan.generated_at) == date(2026, 7, 7)
    log = AuditLog.objects.filter(action=AuditAction.GENERATE, object_id=str(plan.pk)).get()
    assert timezone.localdate(log.occurred_at) == timezone.localdate()


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


def test_replace_is_refused_without_debug(settings: Settings) -> None:
    settings.DEBUG = False
    with pytest.raises(CommandError, match="only for development"):
        seed_scenario("--replace", "--yes")


def test_replace_deletes_the_records_made_on_the_screens() -> None:
    """画面で作った本線と同じ記録（台帳の外）を、紐づく記録ごと消してから作る。

    ダミーデータと監査ログは消さない。
    """
    seed_scenario()
    old_host = HostOrganization.objects.get(name=scenario.HOST_NAME)
    old_worker = ForeignWorker.objects.get(name_romaji=scenario.WORKER_ROMAJI)
    SeedObject.scenario_objects.all().delete()  # 台帳から外す＝画面で作った記録と同じ
    dummy_rows = SeedObject.objects.count()
    dummy_workers = ForeignWorker.objects.exclude(pk=old_worker.pk).count()
    audit_rows = AuditLog.objects.count()
    output = seed_scenario("--replace", "--yes")
    assert "Records made on the screens to delete (by type):" in output
    assert "- hosts.HostOrganization: 1" in output and "- workers.ForeignWorker: 1" in output
    assert "- support.SupportRecord:" in output and "- filings.Filing:" in output
    assert not HostOrganization.objects.filter(pk=old_host.pk).exists()
    assert not ForeignWorker.objects.filter(pk=old_worker.pk).exists()
    assert HostOrganization.objects.filter(name=scenario.HOST_NAME).count() == 1
    assert ForeignWorker.objects.filter(name_romaji=scenario.WORKER_ROMAJI).count() == 1
    assert SeedObject.objects.count() == dummy_rows
    assert ForeignWorker.objects.count() == dummy_workers + 1
    assert AuditLog.objects.count() >= audit_rows  # 監査ログは消さない（作った分が増える）


def test_audit_log_keeps_the_real_time_inside_as_of() -> None:
    """「今」をずらしても、監査ログの日時は実際の時刻。

    AuditLog の既定は、定義のときの timezone.now の関数（ずらした関数ではない）。
    """
    real_day = timezone.localdate()
    worker = ForeignWorker.objects.first()
    assert worker is not None
    with scenario.as_of(date(2026, 7, 7)):
        assert timezone.localdate() == date(2026, 7, 7)
        worker.name_kana = "テスト"
        worker.save()
    log = AuditLog.objects.filter(object_id=str(worker.pk), action=AuditAction.UPDATE).latest("pk")
    assert timezone.localdate(log.occurred_at) == real_day
    assert timezone.localdate() == real_day


def _add_records_made_on_the_screens() -> tuple[int, int, int, int]:
    """画面で足すのと同じ、受入れ機関の書類（事業所つき）・統合 PDF の下書きと生成履歴・ひな形。"""
    from django.core.files.base import ContentFile
    from django.core.files.uploadedfile import SimpleUploadedFile

    from apps.contracts.models import EmploymentConditionTemplate
    from apps.forms_engine.models import SubmissionBundle
    from apps.hosts.models import HostDocument, HostDocumentKind
    from apps.seed.context import DUMMY_PDF
    from services import host_documents, submission_drafts

    host = HostOrganization.objects.get(name=scenario.HOST_NAME)
    worker = ForeignWorker.objects.get(name_romaji=scenario.WORKER_ROMAJI)
    site = host.sites.get()

    def pdf() -> SimpleUploadedFile:
        return SimpleUploadedFile("doc.pdf", DUMMY_PDF, content_type="application/pdf")

    document = host_documents.add(
        HostDocument(
            host_organization=host,
            site=site,
            kind=HostDocumentKind.VARIABLE_HOURS_AGREEMENT,
            period_start=date(2026, 4, 1),
            period_end=date(2027, 3, 31),
        ),
        [pdf()],
        language_ids=[],
        by=None,
    )
    draft = submission_drafts.save(worker, "coe", {"coe_t1-1-1": [pdf()]}, by=None)
    bundle = SubmissionBundle(
        worker=worker,
        set_id="coe",
        definition_valid_from=date(2026, 10, 9),
        basis_on=date(2026, 10, 16),
        sha256="0" * 64,
        size=len(DUMMY_PDF),
        page_count=1,
    )
    bundle.file.save("bundle.pdf", ContentFile(DUMMY_PDF), save=False)
    bundle.save()
    template = EmploymentConditionTemplate.objects.create(host_organization=host, name="ひな形")
    return document.pk, draft.pk, bundle.pk, template.pk


def _gone(document: int, draft: int, bundle: int, template: int) -> None:
    from apps.contracts.models import EmploymentConditionTemplate
    from apps.forms_engine.models import SubmissionBundle, SubmissionDraft, SubmissionDraftFile
    from apps.hosts.models import HostDocument

    assert not HostDocument.objects.filter(pk=document).exists()
    assert not SubmissionDraft.objects.filter(pk=draft).exists()
    assert not SubmissionDraftFile.objects.filter(draft_id=draft).exists()
    assert not SubmissionBundle.objects.filter(pk=bundle).exists()
    assert not EmploymentConditionTemplate.objects.filter(pk=template).exists()
    assert not Attachment.objects.filter(
        content_type__model__in=["hostdocument", "submissiondraft"]
    ).exists()


def test_reset_removes_the_host_store_and_submission_records_made_on_the_screens() -> None:
    """seed の受入れ機関・事業所・外国人に、画面で受入れ機関の書類（保管庫）・統合 PDF の下書き・
    生成履歴・雇用条件のひな形を足した後でも、seed-reset が止まらずに消す（監査ログは残す）。"""
    seed_scenario("--until", "5")
    ids = _add_records_made_on_the_screens()
    audit_rows = AuditLog.objects.count()
    builders.reset()
    _gone(*ids)
    assert not HostOrganization.objects.filter(name=scenario.HOST_NAME).exists()
    assert AuditLog.objects.count() >= audit_rows
    call_command("seed_dummy", "--date", BASE.isoformat(), stdout=StringIO())
    assert SeedObject.objects.exists()


def test_scenario_reset_removes_the_host_store_and_submission_records() -> None:
    seed_scenario("--until", "5")
    ids = _add_records_made_on_the_screens()
    seed_scenario("--reset", "--until", "5")
    _gone(*ids)
    assert HostOrganization.objects.filter(name=scenario.HOST_NAME).count() == 1


def test_replace_counts_and_removes_the_host_store_and_submission_records() -> None:
    seed_scenario("--until", "5")
    ids = _add_records_made_on_the_screens()
    SeedObject.scenario_objects.all().delete()  # 台帳から外す＝画面で作った記録と同じ
    output = seed_scenario("--replace", "--yes", "--until", "5")
    assert "- hosts.HostDocument: 1" in output
    assert "- forms_engine.SubmissionDraft: 1" in output
    assert "- forms_engine.SubmissionBundle: 1" in output
    assert "- contracts.EmploymentConditionTemplate: 1" in output
    assert "- core.Attachment (host documents and submission drafts): 2" in output
    _gone(*ids)


def test_scenario_has_no_empty_field_warnings_on_1_17_and_1_6() -> None:
    """テストシナリオの値（2026-10-20 に足したもの）で、第1-17号・第1-6号の空欄の警告が出ない。"""
    from services import form_data_contract, form_generation, form_samples

    form_templates.load_all()
    seed_scenario("--until", "5")
    worker = ForeignWorker.objects.get(name_romaji=scenario.WORKER_ROMAJI)
    on = date(2026, 7, 10)
    empty_codes = {
        "deduction_without_agreement",
        "work_rules_location_missing",
    }
    version = worker.employment_contracts.get().versions.get()
    targets = {
        "1-17": form_samples.sample("1-17", worker, on),
        "1-6": (version, form_data_contract.build_1_6(version)),
    }
    for form_id, (target, data) in targets.items():
        result = form_generation.check(form_id, target=target, data=data, basis_on=on)
        codes = {finding.code for finding in result.findings}
        assert not {code for code in codes if code.startswith("plan_empty_")}, (form_id, codes)
        assert not codes & empty_codes, (form_id, codes)
    plan = SupportPlan.objects.get(worker=worker).versions.get()
    housing = plan.housing
    assert (housing.occupants, housing.monthly_rent, housing.property_type) == (1, 25000, "leased")
    checks = {check.place: check.municipality for check in plan.municipality_checks.all()}
    assert checks == {"site": "船橋市", "residence": "市川市"}
