"""seed_dummy（ダミーデータ）。件数・再現性・期限の分布・起票・安全装置・リセット。"""

from collections import Counter
from datetime import date, timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from pytest_django.fixtures import Settings

from apps.contracts.forms import VersionForm
from apps.contracts.models import (
    EmploymentConditionTemplate,
    EmploymentContract,
    EmploymentContractVersion,
)
from apps.filings.models import Filing, FilingKind
from apps.hosts.models import HostOrganization, HostSite
from apps.seed.context import DUMMY_USERNAME
from apps.seed.models import SeedObject
from apps.workers.models import (
    ForeignWorker,
    LatestResidenceRecord,
    PassportRecord,
    ResidenceRecord,
)
from services import condition_templates, contracts, corporate_number, passport, residence_card
from tests.core.factories import make_organization
from tests.hosts.factories import make_host
from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db

BASE = date(2026, 10, 16)


@pytest.fixture(autouse=True)
def _allow(settings: Settings) -> None:
    settings.DEBUG = True
    settings.ALLOW_DUMMY_DATA = True


def _seed(*args: str) -> str:
    out = StringIO()
    call_command("seed_dummy", "--date", BASE.isoformat(), *args, stdout=out)
    return out.getvalue()


def _fingerprint() -> list[tuple[str, str, str]]:
    return sorted(
        (
            w.name_romaji,
            w.name_kanji,
            w.latest_residence.record.card_number if hasattr(w, "latest_residence") else "",
        )
        for w in ForeignWorker.objects.all()
    )


def test_counts_and_running_twice_does_not_add_rows() -> None:
    """T12 の完了条件: 何度実行しても同じ件数で再現。"""
    _seed()
    # 31 番は加入証明なしの場面（T39）。1〜30 番の場面とは別
    assert ForeignWorker.objects.count() == 31
    assert Counter(ForeignWorker.objects.values_list("nationality__code", flat=True)) == {
        "CN": 25,
        "VN": 4,
        "ID": 2,
    }
    # 3 社目は転籍の場面（T26）、4 社目は加入証明なしの場面（T39）
    assert HostOrganization.objects.count() == 4
    assert HostSite.objects.count() == 5
    assert EmploymentContract.objects.count() == 32  # 9 番の転籍で 1 つ、31 番で 1 つ増える
    rows = SeedObject.objects.count()
    assert "already exists" in _seed()
    assert SeedObject.objects.count() == rows
    assert ForeignWorker.objects.count() == 31


def test_reset_recreates_the_same_content_for_the_same_base_date() -> None:
    _seed()
    first = _fingerprint()
    filings_before = sorted(Filing.objects.values_list("kind", "basis_on", "due_on"))
    _seed("--reset")
    assert _fingerprint() == first
    assert sorted(Filing.objects.values_list("kind", "basis_on", "due_on")) == filings_before


def test_residence_expiry_distribution() -> None:
    _seed()
    expiries = [
        (latest.record.expires_on - BASE).days
        for latest in LatestResidenceRecord.objects.select_related("record", "worker").exclude(
            worker__status="pre_entry"
        )
    ]
    assert len(expiries) == 29
    assert sum(1 for d in expiries if d < 0) == 1
    assert sum(1 for d in expiries if 0 <= d <= 92) == 5
    assert sum(1 for d in expiries if 92 < d <= 183) == 6
    assert sum(1 for d in expiries if d >= 365) == 17
    histories = Counter(ResidenceRecord.objects.values_list("worker_id", flat=True))
    assert sum(1 for count in histories.values() if count >= 2) == 6
    soon = [p for p in PassportRecord.objects.all() if p.expires_on <= BASE + timedelta(days=183)]
    assert len(soon) == 3


# 1〜29 番の氏名（ローマ字）。30 番の上書き（T26）で、ほかの人の氏名が変わらないことの確認
NAMES_1_TO_29 = [
    "WU XUELI", "WU WEI", "WU YANXIN", "SUN WEIFANG", "HUANG PENGYONG", "LI YANG", "WU JINGMIN",
    "ZHANG HAOJUN", "ZHOU XUEJIE", "MA YANGYAN", "CHEN YONG", "HUANG PENGJUN", "CHEN XUE",
    "ZHU JING", "XU JINGTING", "XU WEILEI", "ZHU PENGLEI", "WU JUNHAO", "LI XUE", "LIU JIE",
    "XU YANG", "HUANG JINGWEI", "WU HUA", "ZHANG JIEPENG", "YANG YAN", "PHAM VAN BINH",
    "NGUYEN VAN HOA", "LE VAN AN", "PUTRI RAHMA",
]  # fmt: skip


def test_names_are_unique_and_only_number_30_is_renamed() -> None:
    _seed()
    ids = SeedObject.objects.filter(content_type__model="foreignworker").values("object_id")
    seeded = list(ForeignWorker.objects.filter(pk__in=ids).order_by("pk"))
    romaji = [w.name_romaji for w in seeded]
    assert romaji[:29] == NAMES_1_TO_29
    assert romaji[29] in ("SARI KURNIAWATI", "RIZKY PRATAMA")
    assert seeded[29].name_native.upper() == romaji[29] and seeded[29].name_kanji == ""
    assert romaji[30] == "NGUYEN VAN AN"  # 31 番（T39。固定の氏名）
    assert len(set(romaji)) == 31


def test_numbers_pass_the_format_checks() -> None:
    _seed()
    cards = [r.card_number for r in ResidenceRecord.objects.all() if r.card_number]
    assert cards and all(residence_card.is_valid(c) for c in cards)
    for record in PassportRecord.objects.all():
        passport.validate(record.number)
    for host in HostOrganization.objects.all():
        assert corporate_number.is_valid(host.corporate_number)
        assert host.corporate_number[1:3] == "99"
        assert "（ダミー）" in host.name


def test_filings_are_created_for_dashboard() -> None:
    _seed()
    kinds = Counter(Filing.objects.values_list("kind", flat=True))
    assert kinds[FilingKind.CONTRACT_TERMINATION] == 3  # 2 人＋9 番の転籍（提出済み。T26）
    assert kinds[FilingKind.ACCEPTANCE_DIFFICULTY] == 1  # 会社都合だけ
    assert kinds[FilingKind.CONTRACT_CHANGE] == 2  # 減給の 2 人（昇給だけの 3 人は起票しない）
    # 在留期限まで 3 か月以内の 6 人のうち、契約を終了した 2 人は「離職」なので在留中の 4 人だけ
    assert kinds[FilingKind.RESIDENCE_RENEWAL] == 4
    assert kinds[FilingKind.PASSPORT_CHECK] == 3
    assert kinds[FilingKind.REGISTRATION_RENEWAL] == 1
    assert kinds[FilingKind.MEMBERSHIP_RENEWAL] == 1
    assert kinds[FilingKind.SUPPORT_CONTRACT_RENEWAL] == 1
    assert kinds[FilingKind.CONTRACT_EXPIRY_CHECK] == 1


def test_staff_and_login_user() -> None:
    _seed()
    from apps.core.models import Staff, User

    assert Staff.objects.count() == 3
    user = User.objects.get(username=DUMMY_USERNAME)
    assert not user.has_usable_password()
    assert Staff.objects.get(user=user)
    assert set(ForeignWorker.objects.values_list("assigned_staff__name", flat=True)) == {
        "佐藤 花子（ダミー）",
        "鈴木 次郎（ダミー）",
    }


@pytest.mark.parametrize(("debug", "allowed"), [(False, True), (True, False)])
def test_refuses_outside_development(settings: Settings, debug: bool, allowed: bool) -> None:
    settings.DEBUG = debug
    settings.ALLOW_DUMMY_DATA = allowed
    with pytest.raises(CommandError):
        _seed()
    with pytest.raises(CommandError):
        _seed("--reset")
    assert not ForeignWorker.objects.exists()


def test_reset_keeps_non_dummy_data() -> None:
    organization = make_organization()
    own_worker = make_worker(name_romaji="REAL TEST")
    own_host = make_host(name="手入力の会社", corporate_number="7000012050002")
    _seed()
    assert ForeignWorker.objects.count() == 32
    _seed("--reset")
    assert ForeignWorker.objects.filter(pk=own_worker.pk).exists()
    assert HostOrganization.objects.filter(pk=own_host.pk).exists()
    assert ForeignWorker.objects.count() == 32
    from apps.core.models import Organization

    assert list(Organization.objects.all()) == [organization]  # 既存の自機関は触らない


def test_every_contract_version_passes_the_current_checks() -> None:
    """seed_dummy が作るすべての雇用契約の版が、業務画面の検査に合っている。

    規則は T28b の validate_version と同じ（saved_version_errors）。検査を足したら、ダミーデータも
    合わせる。
    """
    _seed()
    versions = list(EmploymentContractVersion.objects.select_related("contract", "site"))
    assert versions
    errors = {
        f"{version.contract} v{version.version}": contracts.saved_version_errors(version)
        for version in versions
    }
    assert {key: value for key, value in errors.items() if value} == {}


def test_reset_removes_generated_documents_of_dummy_workers() -> None:
    """ダミーの外国人の生成した帳票（署名の紐づけ・添付・交付記録・届出タスクの参照を含む）も消える。"""
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.utils import timezone

    from apps.forms_engine.models import (
        FormTemplate,
        GeneratedDocument,
        GeneratedDocumentSignature,
        GenerationStatus,
    )
    from apps.support.models import DeliveredBy, DeliveryKind, DeliveryMethod
    from services import deliveries, filings, form_signatures, form_templates
    from tests.conftest import PDF_BYTES

    _seed()
    form_templates.load_all()
    dummy = ForeignWorker.objects.filter(
        pk__in=SeedObject.objects.filter(content_type__model="foreignworker").values("object_id")
    ).order_by("pk")[14]
    version = dummy.support_plans.get().versions.order_by("-version").first()
    template = FormTemplate.objects.get(form_id="1-17", language="ja")
    document = GeneratedDocument.objects.create(
        template=template,
        target=version,
        worker=dummy,
        basis_on=BASE,
        snapshot="{}",
        status=GenerationStatus.COMPLETED,
        generated_at=timezone.now(),
    )
    pdf = SimpleUploadedFile("s.pdf", PDF_BYTES, content_type="application/pdf")
    form_signatures.add_signature(document, [pdf], signed_on=timezone.localdate(), by=None)
    deliveries.create_delivery(
        dummy,
        deliveries.DeliveryInput(
            kind=DeliveryKind.SUPPORT_PLAN_COPY,
            delivered_on=timezone.localdate(),
            method=DeliveryMethod.HAND,
            delivered_by=DeliveredBy.ORGANIZATION,
            explained=True,
            generated_document=document,
        ),
        by=None,
    )
    filing = Filing.objects.filter(worker=dummy).first()
    assert filing is not None
    filings.link_document(filing, document, by=None)
    kept_worker = make_worker(name_romaji="NOT DUMMY")
    kept = GeneratedDocument.objects.create(
        template=template, target=kept_worker, worker=kept_worker, basis_on=BASE, snapshot="{}"
    )

    _seed("--reset")
    assert not GeneratedDocument.objects.filter(pk=document.pk).exists()
    assert not GeneratedDocumentSignature.objects.exists()
    assert GeneratedDocument.objects.filter(pk=kept.pk).exists()  # ダミーでない外国人の分は残る


def test_condition_template_of_the_first_host_fills_a_valid_version() -> None:
    """雇用条件のひな形（T41-D8）: 1 社目に 1 件。印の項目を入れれば、今の検査を通る版になる。"""
    _seed("--date", BASE.isoformat())
    template = EmploymentConditionTemplate.objects.get()
    assert template.host_organization.name.endswith("サンプル精機工業株式会社")
    assert "base_wage" in template.marked and "base_wage" not in template.values
    assert template.values["wage_closing_day"] == "毎月末日"
    initial, rows = condition_templates.initial_for(template)
    assert "duties" not in initial and rows["deductions"][0]["amount"] == 25000
    form = VersionForm(
        {
            **{
                key: value for key, value in initial.items() if not isinstance(value, bool) or value
            },
            "starts_on": "2026-11-01",
            "ends_on": "2027-10-31",
            "base_wage": "230000",
            "duties": "製造工程の作業（ダミー）",
            "hourly_equivalent": str(round(230000 * 60 / 9800)),
        }
    )
    assert form.is_valid(), form.errors
    _seed("--reset", "--date", BASE.isoformat())
    assert EmploymentConditionTemplate.objects.count() == 1
