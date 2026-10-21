"""申請の書類（第3-1号・第13-1号・第13-2号・第5-10号・第1-25号）と申請の書類の一覧（T37、ADR-0026）。

- テンプレート・マッピング・産業の一覧・申請の書類の一覧は道具の出力そのもの（手で直さない）。
- 誓約書の固定の文が述べる事実と記録が食い違えば生成不可、述べない事実は警告（T37-D5）。
- 作成責任者は画面で変えられ、変えた値がスナップショットに残る（T37-D10）。
- 委託料の内訳だけの変更では第3-3-1号を起票しない（T37-D9）。
"""

import datetime
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.contracts.models import EmploymentContract, VariableHoursUnit
from apps.core.models import AttachmentKind, AuditAction, AuditLog, Country, Organization
from apps.filings.models import Filing
from apps.forms_engine.models import GeneratedDocument, GenerationStatus
from apps.hosts.models import EntityType, FeeTiming, HostMembership, SupportContract
from apps.workers.models import ApplicationType, StatusOfResidence
from services import (
    application_sets,
    attachments,
    form_data_application,
    form_docx,
    form_generation,
    host_records,
    industries,
    jobs,
)
from services.attachments import create_attachment
from tests.contracts.factories import make_contract
from tests.core.factories import make_organization, make_user, verified_client
from tests.forms_engine import doc_conversion, pdfcheck
from tests.forms_engine.test_form_contract import body_texts
from tests.hosts.factories import category, make_host, make_site
from tests.workers.factories import add_residence, make_worker

pytestmark = pytest.mark.django_db

ROOT = Path(settings.FORM_TEMPLATES_ROOT)
MAPPINGS = Path(settings.FORM_MAPPINGS_ROOT)
REPO = ROOT.parent.parent
TOOLS = REPO / "tools" / "forms"
VALID_FROM = "2026-10-18"
BASIS = datetime.date(2026, 10, 18)
FORMS = ("3-1", "13-1", "13-2", "5-10", "1-25")
SOURCES = {"1-25": "converted.docx"}
TAG = re.compile(r'\{\{ (\w+) or "([^"]*)" \}\}')
PDF = b"%PDF-1.4\n%dummy\n"


def folder(form_id: str) -> Path:
    return ROOT / form_id / VALID_FROM


def pdf(name: str = "certificate.pdf") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, PDF, content_type="application/pdf")


# --- 道具の出力 -------------------------------------------------------------------------------


@pytest.mark.parametrize("form_id", FORMS)
def test_templates_and_mappings_are_reproduced_by_the_tool(tmp_path: Path, form_id: str) -> None:
    source = SOURCES.get(form_id, "original.docx")
    shutil.copyfile(folder(form_id) / source, tmp_path / source)
    result = subprocess.run(
        [sys.executable, str(TOOLS / "make_application_templates.py"), form_id, str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert (tmp_path / "template.docx").read_bytes() == (
        folder(form_id) / "template.docx"
    ).read_bytes()
    assert result.stdout == (MAPPINGS / form_id / f"{VALID_FROM}.json").read_text("utf-8")


@pytest.mark.parametrize("form_id", FORMS)
def test_template_rendered_empty_is_the_original(tmp_path: Path, form_id: str) -> None:
    out = tmp_path / "empty.docx"
    form_docx.render(folder(form_id) / "template.docx", out, {})
    assert body_texts(out) == body_texts(folder(form_id) / SOURCES.get(form_id, "original.docx"))


@pytest.mark.parametrize("form_id", FORMS)
def test_mapping_targets_are_the_tags_of_the_template(form_id: str) -> None:
    with zipfile.ZipFile(folder(form_id) / "template.docx") as archive:
        tags = {name for name, _o in TAG.findall(archive.read("word/document.xml").decode())}
    mapping = json.loads((MAPPINGS / form_id / f"{VALID_FROM}.json").read_text("utf-8"))
    assert {item["target"] for item in mapping["fields"]} == tags


def test_industry_lists_are_reproduced_by_the_tool() -> None:
    result = subprocess.run(
        [sys.executable, str(TOOLS / "make_industry_lists.py")],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == industries.DATA_FILE.read_text("utf-8")
    lists = industries.lists()
    assert len(lists["3-1"]) == 76 and len(lists["13-1"]) == 10


def test_application_sets_are_reproduced_by_the_tool(tmp_path: Path) -> None:
    subprocess.run(
        [sys.executable, str(TOOLS / "make_application_sets.py"), str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    for set_id in application_sets.SET_IDS:
        name = f"{set_id}/2026-10-09.json"
        assert (tmp_path / name).read_text("utf-8") == (REPO / "application_sets" / name).read_text(
            "utf-8"
        )
    counts = {
        set_id: len(application_sets.definition(set_id, BASIS).rows)
        for set_id in application_sets.SET_IDS
    }
    assert counts == {"coe": 68, "chg": 76, "ext": 21}
    ids = [
        row["id"]
        for set_id in application_sets.SET_IDS
        for row in application_sets.definition(set_id, BASIS).rows
    ]
    assert len(ids) == len(set(ids))


# --- 第1-25号の .doc の変換（T34-D1 と同じ確かめ方）----------------------------------------


def test_converted_1_25_has_the_same_pages_and_characters_as_the_doc() -> None:
    from_doc = pdfcheck.doc_to_pdf(folder("1-25") / "original.doc")
    blank = pdfcheck.blank_pdf(folder("1-25") / "converted.docx")
    assert len(pdfcheck.pdf_text(from_doc)) == len(pdfcheck.pdf_text(blank))
    squash = re.compile(r"\s+")
    assert squash.sub("", "".join(pdfcheck.pdf_text(from_doc))) == squash.sub(
        "", "".join(pdfcheck.pdf_text(blank))
    )


def test_converted_1_25_has_the_same_tables_as_the_doc(tmp_path: Path) -> None:
    from tests.forms_engine.test_form_contract import _odt_tables

    shutil.copyfile(folder("1-25") / "original.doc", tmp_path / "original.doc")
    from_doc = _odt_tables(tmp_path / "original.doc", tmp_path)
    xml = zipfile.ZipFile(folder("1-25") / "converted.docx").read("word/document.xml").decode()
    tables = re.findall(r"<w:tbl>.*?</w:tbl>", xml, re.S)
    from_docx = [
        (
            len(re.findall(r"<w:tr[ >]", table)),
            len(re.findall(r"<w:gridSpan", table)),
            len(re.findall(r'<w:vMerge w:val="restart"', table)),
        )
        for table in tables
    ]
    assert from_doc == from_docx


def test_converting_1_25_again_gives_the_same_xml(tmp_path: Path) -> None:
    convert_doc = doc_conversion.convert_doc_or_skip(folder("1-25"))
    out = tmp_path / "again.docx"
    convert_doc.convert(folder("1-25") / "original.doc", out)
    with zipfile.ZipFile(out) as again, zipfile.ZipFile(folder("1-25") / "converted.docx") as reg:
        names = [name for name in reg.namelist() if name.startswith("word/")]
        assert names
        for name in names:
            assert again.read(name) == reg.read(name), name


# --- 産業の照合 ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("form_id", "code", "result"),
    [
        ("3-1", "2194", "match"),
        ("3-1", "2591", "none"),  # 中分類 25 の除外
        ("3-1", "2599", "match"),
        ("3-1", "2299", "limited"),  # 鉄粉製造業に限る
        ("3-1", "2922", "limited"),  # 2922 のうち自動車用以外を除く
        ("3-1", "2911", "match"),
        ("3-1", "13", "coarse"),
        ("3-1", "0911", "none"),
        ("13-1", "0999", "match"),
        ("13-1", "5811", "limited"),
        ("13-1", "5861", "match"),
        ("13-1", "2194", "none"),
    ],
)
def test_industry_match(form_id: str, code: str, result: str) -> None:
    assert industries.match(form_id, code).result == result


# --- 受入れ機関・自機関・契約の組 -------------------------------------------------------------


def make_case(
    field_code: str = "industrial_products",
    *,
    industry: str | None = None,
    full_support: bool = True,
    with_fees: bool = True,
    entity_type: str = EntityType.CORPORATION,
    **contract_values: Any,
) -> EmploymentContract:
    """自機関（協議会の加入・証明書あり）・受入れ機関（加入と証明書）・支援委託契約・雇用契約。"""
    organization = make_organization(
        representative_title="代表理事",
        representative_name="鈴木 花子",
        court_name="東京",
        food_council_joined_on=datetime.date(2025, 1, 1),
    )
    create_attachment(
        target=organization,
        kind=AttachmentKind.FOOD_COUNCIL_CERTIFICATE,
        file=pdf(),
        created_by=None,
    )
    host_values: dict[str, Any] = {"entity_type": entity_type}
    if entity_type == EntityType.SOLE_PROPRIETOR:
        host_values["corporate_number"] = ""
    host = make_host(**host_values)
    food = field_code == "food_manufacturing"
    code = industry or ("0999" if food else "2194")
    site = make_site(host, field_code, industry_code=code)
    category_code = "food_manufacturing_general" if food else "machine_metal_processing"
    site.categories.set([category(category_code)])
    contract = make_contract(make_worker(), site=site, **contract_values)
    contract.versions.get().categories.set([category(category_code)])
    membership = HostMembership.objects.create(
        host_organization=host, field=site.field, joined_on=datetime.date(2025, 1, 1)
    )
    create_attachment(
        target=membership, kind=AttachmentKind.MEMBERSHIP_CERTIFICATE, file=pdf(), created_by=None
    )
    if full_support:
        support = SupportContract.objects.create(
            host_organization=host,
            monthly_fee=30000,
            concluded_on=datetime.date(2026, 3, 1),
            starts_on=datetime.date(2026, 3, 15),
            ends_on=datetime.date(2027, 3, 14),
        )
        if with_fees:
            host_records.save_fees(support, fee_rows(), by=None)
    return contract


def fee_rows(first: int = 25000, second: int = 5000) -> list[host_records.FeeInput]:
    return [
        host_records.FeeInput(name="義務的支援", amount=first, timing=FeeTiming.REGULAR),
        host_records.FeeInput(name="通訳", amount=second, timing=FeeTiming.OCCASIONAL),
    ]


def check(form_id: str, contract: EmploymentContract, author: str | None = None) -> Any:
    if author is None:
        author = form_data_application.default_author(form_id, contract)
    data = form_data_application.build(form_id, contract, BASIS, author=author)
    return form_generation.check(form_id, target=contract, data=data, basis_on=BASIS)


def codes(result: Any) -> dict[str, str]:
    return {finding.code: finding.severity for finding in result.findings}


# --- 検査 ---------------------------------------------------------------------------------------


def test_complete_cases_have_no_findings(loaded: None) -> None:
    industrial = make_case()
    assert codes(check("3-1", industrial)) == {}
    assert codes(check("5-10", industrial)) == {}
    assert codes(check("1-25", industrial)) == {}


def test_pledge_contradictions_block(loaded: None) -> None:
    contract = make_case("food_manufacturing")
    assert codes(check("3-1", contract))["field_differs"] == "block"
    site = contract.versions.get().site
    site.industry_code = "2194"
    site.save()
    assert codes(check("13-1", contract))["industry_not_listed"] == "block"
    site.industry_code = "5811"
    site.save()
    assert codes(check("13-1", contract)) == {
        "industry_limited": "warn",
        "career_plan_missing": "warn",
    }
    for attachment in HostMembership.objects.get().attachments.all():
        attachments.void_attachment(attachment, by=None, reason="誤り")
    assert codes(check("13-1", contract))["certificate_missing"] == "block"


def test_own_council_certificate_is_needed_for_13_2_and_full_support(loaded: None) -> None:
    contract = make_case("food_manufacturing")
    organization = Organization.objects.get()
    organization.food_council_joined_on = None
    organization.save()
    assert codes(check("13-2", contract))["own_certificate_missing"] == "block"
    assert codes(check("13-1", contract))["own_certificate_missing"] == "block"
    SupportContract.objects.all().delete()
    assert "own_certificate_missing" not in codes(check("13-1", contract))  # 全部委託でない
    assert codes(check("13-2", contract))["support_contract_missing"] == "warn"


def test_support_contract_checks(loaded: None) -> None:
    contract = make_case(with_fees=False)
    support = SupportContract.objects.get()
    support.ends_on = None
    support.save()
    organization = Organization.objects.get()
    organization.court_name = ""
    organization.registration_number = "登録番号未定"
    organization.save()
    assert codes(check("5-10", contract)) == {
        "support_contract_no_end": "warn",
        "fee_breakdown_missing": "warn",
        "court_missing": "warn",
    }
    assert codes(check("1-25", contract)) == {
        "support_contract_no_end": "warn",
        "registration_number_format": "warn",
    }
    assert codes(check("1-25", contract, author=""))["required"] == "block"
    SupportContract.objects.all().delete()
    assert codes(check("5-10", contract))["support_contract_missing"] == "block"


# --- データ ------------------------------------------------------------------------------------


def test_data_of_5_10_and_1_25(loaded: None) -> None:
    contract = make_case()
    data = form_data_application.build("5-10", contract, BASIS, author="")
    assert data["a.field"] == "工業製品製造業"
    assert data["a.monthly_fee"] == 30000 and data["a.fee_total"] == 30000
    assert data["a.fee2_name"] == "通訳" and data["a.fee2_occasional"] is True
    assert data["a.court"] == "東京"
    assert data["a.organization_signature"] == "テスト登録支援機関　代表理事　鈴木 花子"
    data = form_data_application.build("1-25", contract, BASIS, author="代表取締役　山田 太郎")
    assert data["a.registration_year"] == "19" and data["a.registration_serial"] == "000000"
    assert data["a.full_support"] == "該当" and data["a.monthly_fee_text"] == "30,000円"
    assert form_data_application.default_author("13-2", contract) == "代表理事　鈴木 花子"
    assert form_data_application.default_author("3-1", contract) == "代表取締役　山田 太郎"


# --- 生成 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("form_id", "field_code"),
    [
        ("3-1", "industrial_products"),
        ("13-1", "food_manufacturing"),
        ("13-2", "food_manufacturing"),
        ("5-10", "industrial_products"),
        ("1-25", "industrial_products"),
    ],
)
def test_generation_keeps_the_pages_and_the_author(
    loaded: None, form_id: str, field_code: str
) -> None:
    contract = make_case(field_code)
    data = form_data_application.build(form_id, contract, BASIS, author="工場長　佐藤 次郎")
    document = form_generation.request(
        form_id, target=contract, data=data, basis_on=BASIS, by=None, accept_warnings=True
    )
    document.refresh_from_db()
    assert document.status == GenerationStatus.COMPLETED, document.failure_reason
    assert document.worker == contract.worker
    blank = pdfcheck.blank_from_template(folder(form_id) / "template.docx")
    assert document.page_count == len(pdfcheck.pdf_text(blank))
    text = "\n".join(body_texts(Path(document.filled_file.path)))
    assert contract.worker.name_romaji in text or form_id == "13-2"
    snapshot = json.loads(document.snapshot)
    if form_id == "5-10":
        assert "a.author" not in snapshot["data"]
        assert "義務的支援" in text
    else:
        assert snapshot["data"]["a.author"].strip() == "工場長　佐藤 次郎"  # T37-D10
        assert "佐藤 次郎" in text


# --- 委託料の内訳（T37-D9）------------------------------------------------------------------


def test_fee_breakdown_must_match_the_monthly_fee_and_raises_no_filing(loaded: None) -> None:
    make_case(with_fees=False)
    support = SupportContract.objects.get()
    with pytest.raises(ValidationError):
        host_records.save_fees(support, fee_rows(20000, 5000), by=None)  # 合計が月額と違う
    with pytest.raises(ValidationError):
        host_records.save_fees(support, [], by=None)
    count = Filing.objects.count()
    host_records.save_fees(support, fee_rows(), by=None)
    host_records.save_fees(support, fee_rows(20000, 10000), by=None)  # 内訳だけ変える
    assert Filing.objects.count() == count  # 第3-3-1号を起票しない（判定は月額）
    assert [fee.amount for fee in support.fees.order_by("order")] == [20000, 10000]
    assert (
        AuditLog.objects.filter(
            content_type__model="supportcontractfee", action=AuditAction.DELETE
        ).count()
        == 2
    )


def contract_post(support: SupportContract, token: tuple[str, str]) -> dict[str, str]:
    return {
        token[0]: token[1],
        "concluded_on": support.concluded_on.isoformat() if support.concluded_on else "",
        "starts_on": support.starts_on.isoformat(),
        "ends_on": support.ends_on.isoformat() if support.ends_on else "",
        "monthly_fee": str(support.monthly_fee),
    }


def test_fee_breakdown_is_saved_with_the_support_contract(loaded: None) -> None:
    """内訳は支援委託契約の画面で契約と一緒に保存する（修正 1 回目 B11）。"""
    make_case(with_fees=False)
    support = SupportContract.objects.get()
    client = verified_client(make_user("tanto37", roles=("support_staff",)))
    base = f"/hosts/{support.host_organization_id}/support-contracts/{support.pk}"
    # 旧い内訳の画面の URL（生成前の検査の「直す」）は、契約の画面に移って内訳を開く
    old = client.get(f"{base}/fees/")
    assert old.status_code == 302 and old["Location"] == f"{base}/edit/?fees=open"
    url = f"{base}/edit/"
    page = client.get(url + "?fees=open")
    assert page.context["fees_open"] is True
    token = (page.context["once_field"], page.context["once_token"])
    rows = {
        "fees-TOTAL_FORMS": "2",
        "fees-INITIAL_FORMS": "0",
        "fees-0-name": "義務的支援",
        "fees-0-amount": "25000",
        "fees-0-timing": FeeTiming.REGULAR,
        "fees-1-name": "通訳",
        "fees-1-amount": "4000",
        "fees-1-timing": FeeTiming.OCCASIONAL,
    }
    response = client.post(url, {**contract_post(support, token), **rows})
    assert response.status_code == 200 and not support.fees.exists()  # 合計が違う
    assert response.context["fees_open"] is True
    assert len(response.context["fee_errors"]) == 1
    assert 'id="fee-errors"' in response.content.decode()
    page = client.get(url)
    token = (page.context["once_field"], page.context["once_token"])
    rows["fees-1-amount"] = "5000"
    response = client.post(url, {**contract_post(support, token), **rows})
    assert response.status_code == 302
    assert support.fees.count() == 2
    created = AuditLog.objects.filter(
        content_type__model="supportcontractfee", action=AuditAction.CREATE
    ).count()
    # 内訳の行を送らない保存（内訳の欄の無い送信）は内訳を変えない。同じ内訳の保存は行を作り直さない
    page = client.get(url)
    response = client.post(
        url, contract_post(support, (page.context["once_field"], page.context["once_token"]))
    )
    assert response.status_code == 302 and support.fees.count() == 2
    page = client.get(url)
    token = (page.context["once_field"], page.context["once_token"])
    assert client.post(url, {**contract_post(support, token), **rows}).status_code == 302
    assert (
        AuditLog.objects.filter(
            content_type__model="supportcontractfee", action=AuditAction.CREATE
        ).count()
        == created
    )
    # 行を全部消すと内訳なし
    page = client.get(url)
    token = (page.context["once_field"], page.context["once_token"])
    empty = {"fees-TOTAL_FORMS": "0", "fees-INITIAL_FORMS": "0"}
    assert client.post(url, {**contract_post(support, token), **empty}).status_code == 302
    assert not support.fees.exists()


# --- 申請の書類の一覧 ---------------------------------------------------------------------------


def test_default_set_follows_the_residence(loaded: None) -> None:
    worker = make_worker()
    assert application_sets.default_set(worker) == "coe"
    add_residence(worker, status_of_residence=StatusOfResidence.TECHNICAL_INTERN_2)
    assert application_sets.default_set(worker) == "chg"
    add_residence(
        worker,
        status_of_residence=StatusOfResidence.SSW_1,
        application_type=ApplicationType.CHANGE,
        permitted_on=datetime.date(2026, 5, 1),
        expires_on=datetime.date(2027, 5, 1),
    )
    assert application_sets.default_set(worker) == "ext"


def by_id(overview: application_sets.Overview) -> dict[str, application_sets.RowState]:
    return {state.row["id"]: state for state in overview.rows + overview.not_applicable}


def test_rows_apply_by_host_field_support_and_nationality(loaded: None) -> None:
    contract = make_case("food_manufacturing", entity_type=EntityType.SOLE_PROPRIETOR)
    worker = contract.worker
    worker.nationality = Country.objects.get(code="KH")
    worker.save()
    found = by_id(application_sets.overview(worker, "coe", BASIS))
    assert found["coe_t2_3-1-1"].applicable is True  # 個人事業主
    assert found["coe_t2_2-1-1"].applicable is False  # 法人の行
    assert found["coe_food-4-1"].applicable is True  # 全部委託 → 第13-2号
    assert found["coe_t2_3-2-1"].applicable is False  # 全部委託しない場合の行
    assert found["coe_ind-2-1"].applicable is False
    assert found["coe_t1-12-1"].applicable is True  # カンボジア
    assert found["coe_t1-7-3"].applicable is False  # 1 年単位の変形労働時間制でない
    assert found["coe_t2_1-1-1"].status == application_sets.Status.ALTERNATIVE
    other = make_contract(
        make_worker(),
        site=contract.versions.get().site,
        variable_hours_unit=VariableHoursUnit.ONE_YEAR,
    )
    worker = other.worker
    found = by_id(application_sets.overview(worker, "coe", BASIS))
    assert found["coe_t1-7-3"].applicable is True
    assert found["coe_t1-7-3"].status == application_sets.Status.MISSING


def test_row_status_follows_generation_and_attachments(loaded: None) -> None:
    contract = make_case()
    worker = contract.worker
    found = by_id(application_sets.overview(worker, "chg", BASIS))
    assert found["chg_ind-2-1"].status == application_sets.Status.NOT_GENERATED
    assert found["chg_ind-3-1"].status == application_sets.Status.ATTACHED
    assert found["chg_t1-8-1"].status == application_sets.Status.MISSING  # 健康診断個人票
    data = form_data_application.build("3-1", contract, BASIS, author="代表取締役　山田 太郎")
    form_generation.request("3-1", target=contract, data=data, basis_on=BASIS, by=None)
    health = create_attachment(
        target=worker,
        kind=AttachmentKind.HEALTH_CHECK_RECORD,
        file=pdf("secret-health.pdf"),
        created_by=None,
    )
    health.examined_on = BASIS  # 受診日（修正 1 回目 B8）
    health.save()
    membership = HostMembership.objects.get()
    membership.valid_until = datetime.date(2026, 9, 30)
    membership.save()
    found = by_id(application_sets.overview(worker, "chg", BASIS))
    assert found["chg_ind-2-1"].status == application_sets.Status.GENERATED
    assert found["chg_t1-8-1"].status == application_sets.Status.ATTACHED
    assert found["chg_ind-3-1"].status == application_sets.Status.EXPIRED
    # 画面: 監査ログに 1 件、健康診断の書類のファイル名は出さない
    client = verified_client(make_user("tanto37", roles=("support_staff",)))
    before = AuditLog.objects.filter(action=AuditAction.VIEW).count()
    page = client.get(f"/workers/{worker.pk}/documents/application/?set_id=chg&basis_on=2026-10-18")
    content = page.content.decode()
    assert page.status_code == 200
    assert 'data-row="chg_ind-2-1" data-status="generated"' in content
    assert "secret-health" not in content
    assert AuditLog.objects.filter(action=AuditAction.VIEW).count() == before + 1
    log = AuditLog.objects.filter(action=AuditAction.VIEW).latest("pk")
    assert log.context["shown"]["application_set"] == "chg"
    tab = client.get(f"/workers/{worker.pk}/documents/").content.decode()
    assert 'id="application-set-link"' in tab


def test_generation_screen_keeps_a_changed_author(loaded: None) -> None:
    contract = make_case()
    client = verified_client(make_user("tanto37", roles=("support_staff",)))
    url = f"/workers/{contract.worker_id}/contracts/{contract.pk}/documents/1-25/"
    page = client.get(url)
    assert page.context["form"].initial["author"] == "代表取締役　山田 太郎"
    tab = client.get(f"/workers/{contract.worker_id}/contracts/").content.decode()
    assert f"/contracts/{contract.pk}/documents/3-1/" in tab
    assert f"/contracts/{contract.pk}/documents/13-1/" not in tab  # 工業の契約
    client.post(
        url,
        {
            page.context["once_field"]: page.context["once_token"],
            "basis_on": BASIS.isoformat(),
            "author": "人事部長　田中 一郎",
            "action": "generate",
        },
    )
    document = GeneratedDocument.objects.get(template__form_id="1-25")
    assert json.loads(document.snapshot)["data"]["a.author"] == "　人事部長　田中 一郎"


# --- 健康診断の受診の時期（修正 1 回目 B8。運用要領 本体 印刷16頁）----------------------------


@pytest.mark.parametrize(
    ("set_id", "examined_on", "status"),
    [
        ("coe", datetime.date(2026, 8, 1), application_sets.Status.ATTACHED),  # 3 か月以内
        ("coe", datetime.date(2026, 7, 1), application_sets.Status.OUT_OF_PERIOD),
        ("chg", datetime.date(2026, 7, 1), application_sets.Status.ATTACHED),  # 1 年以内
        ("chg", datetime.date(2025, 10, 1), application_sets.Status.OUT_OF_PERIOD),
        ("coe", None, application_sets.Status.DATE_MISSING),
    ],
)
def test_health_check_period_depends_on_the_application(
    loaded: None, set_id: str, examined_on: datetime.date | None, status: str
) -> None:
    contract = make_case()
    attachment = create_attachment(
        target=contract.worker, kind=AttachmentKind.HEALTH_CHECK_RECORD, file=pdf(), created_by=None
    )
    if examined_on is not None:
        attachment.examined_on = examined_on
        attachment.save()
    rows = {
        state.row["id"]: state
        for state in application_sets.overview(contract.worker, set_id, BASIS).rows
    }
    row_id = "coe_t1-9-1" if set_id == "coe" else "chg_t1-8-1"
    assert rows[row_id].status == status
    ext_rows = application_sets.overview(contract.worker, "ext", BASIS).rows
    assert not any(s.row["how"].get("attachment") == "health_check" for s in ext_rows)


def test_rows_have_a_tone_and_show_generation_in_progress(
    loaded: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """行の色の区分と、生成を依頼した後・ジョブが終わる前の「生成中」（修正 1 回目 B9・B10）。"""
    contract = make_case()
    worker = contract.worker
    found = by_id(application_sets.overview(worker, "chg", BASIS))
    assert found["chg_ind-2-1"].tone == "danger"  # 未生成
    assert found["chg_ind-3-1"].tone == "ok"  # 添付済み
    assert all(state in application_sets.TONES for state in application_sets.Status)
    monkeypatch.setattr(jobs, "enqueue", lambda *args: None)  # ジョブを動かさない
    data = form_data_application.build("3-1", contract, BASIS, author="代表取締役　山田 太郎")
    form_generation.request("3-1", target=contract, data=data, basis_on=BASIS, by=None)
    state = by_id(application_sets.overview(worker, "chg", BASIS))["chg_ind-2-1"]
    assert (state.status, state.tone) == (application_sets.Status.GENERATING, "muted")
    client = verified_client(make_user("tanto37", roles=("support_staff",)))
    content = client.get(
        f"/workers/{worker.pk}/documents/application/?set_id=chg&basis_on=2026-10-18"
    ).content.decode()
    row = re.search(r'<tr data-row="chg_ind-2-1"[^>]*>', content)
    assert row is not None  # 属性ごとに確かめる（並びに頼らない。U7-D1）
    assert 'data-status="generating"' in row.group(0) and 'data-tone="muted"' in row.group(0)
    # 生成のリンクはモーダルで開き、終わったら一覧に戻る
    assert "data-modal data-modal-return" in content


def test_modal_requests_get_the_redirect_as_a_header(loaded: None) -> None:
    """モーダルの中の送信のリダイレクトは 204 と X-Modal-Location（修正 1 回目 B10）。"""
    make_case(with_fees=False)
    support = SupportContract.objects.get()
    client = verified_client(make_user("tanto37", roles=("support_staff",)))
    base = f"/hosts/{support.host_organization_id}/support-contracts/{support.pk}"
    response = client.get(f"{base}/fees/", headers={"X-Modal": "1"})
    assert response.status_code == 204
    assert response["X-Modal-Location"] == f"{base}/edit/?fees=open"
    assert client.get(f"{base}/fees/").status_code == 302
