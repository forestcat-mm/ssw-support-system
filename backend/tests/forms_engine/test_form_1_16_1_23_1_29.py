"""第1-16号・第1-23号・第1-29号（T54。ADR-0043）。

段階3の様式と同じ確かめ方: ① 道具の出力の再現・空で差し込むと公式の様式と同じ本文と頁数・
生成前の検査と生成 ② 画面での生成（申請の書類の一覧の行から）。
"""

import datetime
import json
import subprocess
import sys
from pathlib import Path

import pytest
from django.conf import settings

from apps.forms_engine.models import GeneratedDocument, GenerationStatus
from apps.hosts.models import HostOfficer
from apps.support.models import Briefing, BriefingForeignFee
from services import application_sets, form_data_application, form_docx, form_generation
from tests.core.factories import make_staff, make_user, verified_client
from tests.forms_engine import pdfcheck
from tests.forms_engine.test_form_application import make_case
from tests.forms_engine.test_form_contract import body_texts

pytestmark = pytest.mark.django_db

ROOT = Path(settings.FORM_TEMPLATES_ROOT)
MAPPINGS = Path(settings.FORM_MAPPINGS_ROOT)
TOOLS = ROOT.parent.parent / "tools" / "forms"
VERSION = "2026-10-21"
BASIS = datetime.date(2026, 10, 21)
FORMS = ("1-16", "1-23", "1-29")


@pytest.mark.parametrize("form_id", FORMS)
def test_template_and_mapping_are_reproduced_by_the_tool(form_id: str, tmp_path: Path) -> None:
    folder = tmp_path / form_id
    folder.mkdir()
    source = ROOT / form_id / VERSION
    (folder / "original.docx").write_bytes((source / "original.docx").read_bytes())
    result = subprocess.run(
        [sys.executable, str(TOOLS / "make_application_templates.py"), form_id, str(folder)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert (folder / "template.docx").read_bytes() == (source / "template.docx").read_bytes()
    assert json.loads(result.stdout) == json.loads(
        (MAPPINGS / form_id / f"{VERSION}.json").read_text("utf-8")
    )


@pytest.mark.parametrize("form_id", FORMS)
def test_empty_template_is_the_official_form(form_id: str, tmp_path: Path) -> None:
    source = ROOT / form_id / VERSION
    out = tmp_path / "empty.docx"
    form_docx.render(source / "template.docx", out, {})
    assert body_texts(out) == body_texts(source / "original.docx")
    blank = pdfcheck.blank_pdf(source / "original.docx")
    empty = pdfcheck.blank_from_template(source / "template.docx")
    assert len(pdfcheck.pdf_text(blank)) == len(pdfcheck.pdf_text(empty))


def _generate(form_id: str, contract: object, data: dict[str, object]) -> GeneratedDocument:
    document = form_generation.request(
        form_id,
        target=contract,  # type: ignore[arg-type]
        data=data,
        basis_on=BASIS,
        by=None,
    )
    document.refresh_from_db()
    assert document.status == GenerationStatus.COMPLETED, document.failure_reason
    return document


def _text(document: GeneratedDocument) -> str:
    return "".join(pdfcheck.pdf_text(Path(document.pdf_file.path)))


def test_1_29_and_1_23(loaded: None) -> None:
    contract = make_case()
    host = contract.host_organization
    data = form_data_application.build("1-29", contract, BASIS, author="")
    assert "書類省略に当たっての誓約書" in _text(_generate("1-29", contract, data))
    # 第1-23号: 関与しない役員が無ければ生成不可
    data = form_data_application.build("1-23", contract, BASIS, author="代表取締役　山田 太郎")
    result = form_generation.check("1-23", target=contract, data=data, basis_on=BASIS)
    assert [f.rule for f in result.blocking] == ["officer_required"]
    HostOfficer.objects.create(
        host_organization=host, name="山田 太郎", name_kana="やまだ たろう", position="代表取締役"
    )
    HostOfficer.objects.create(
        host_organization=host,
        name="佐藤 花子",
        name_kana="さとう はなこ",
        position="監査役",
        not_involved_in_acceptance=True,
    )
    data = form_data_application.build("1-23", contract, BASIS, author="代表取締役　山田 太郎")
    assert data["a.officer1_name"] == "佐藤 花子" and "a.officer2_name" not in data
    text = _text(_generate("1-23", contract, data))
    assert "佐藤" in text and "さとう" in text


def test_1_16_inputs_and_defaults(loaded: None) -> None:
    contract = make_case()
    worker = contract.worker
    defaults = form_data_application.hiring_defaults(contract, BASIS)
    assert defaults == {"guidance": ""}
    inputs = {
        "placement": "yes",
        "license_number": "13-ユ-123456",
        "license_accepted_on": datetime.date(2020, 4, 1),
        "placement_kind": "paid",
        "placement_name": "株式会社サンプル人材",
        "placement_applicant_yen": 0,
        "agency": "no",
        "guidance": "yes",
        "paid1_name": "送出機関A",
        "paid1_purpose": "研修費",
        "paid1_date": datetime.date(2026, 1, 10),
        "paid1_amount": "USD 300",
        "paid1_yen": 45000,
    }
    data = form_data_application.build_1_16(contract, BASIS, author="代表取締役", inputs=inputs)
    assert data["a.placement_yes"] is True and data["a.placement_paid"] is True
    assert data["a.guidance"] == "有" and data["a.paid_total_yen"] == 45000
    text = _text(_generate("1-16", contract, data))
    for value in ("株式会社サンプル人材", "送出機関A", "４５，０００", "13-ユ-123456"):
        assert value in text, value
    # 前の生成の入力が次の初期値になる
    again = form_data_application.hiring_defaults(contract, BASIS)
    assert again["placement_name"] == "株式会社サンプル人材"
    assert again["paid1_date"] == "2026-01-10"
    assert worker.pk == GeneratedDocument.objects.get(template__form_id="1-16").worker_id


def test_1_16_defaults_from_the_guidance(loaded: None) -> None:
    from apps.support.models import SupportRecord
    from services.support_catalog import SupportItem

    contract = make_case()
    record = SupportRecord.objects.create(
        worker=contract.worker,
        kind=SupportItem.PRE_GUIDANCE,
        occurred_on=datetime.date(2026, 3, 1),
        staff=make_staff(),
    )
    briefing = Briefing.objects.create(
        record=record, worker=contract.worker, checklist_version="v1", place="オンライン"
    )
    BriefingForeignFee.objects.create(
        briefing=briefing,
        organization_name="送出機関B",
        paid_on=datetime.date(2026, 2, 1),
        amount_jpy=30000,
        breakdown="手数料",
    )
    defaults = form_data_application.hiring_defaults(contract, BASIS)
    assert defaults["guidance"] == "yes"
    assert (defaults["paid1_name"], defaults["paid1_yen"]) == ("送出機関B", 30000)


def test_application_set_rows_generate_the_forms(loaded: None) -> None:
    contract = make_case()
    overview = application_sets.overview(contract.worker, "coe", BASIS)
    generated = {s.row["how"].get("generate"): s for s in overview.rows + overview.not_applicable}
    assert generated["1-16"].action_url.endswith(f"/contracts/{contract.pk}/documents/1-16/")
    # 第1-29号の行は実績のある機関の書類（システムでは判断しない）。生成の画面へは行ける
    assert generated["1-29"].action_url.endswith(f"/contracts/{contract.pk}/documents/1-29/")
    # 第1-23号の行は、関与しない役員がいれば当てはまる
    assert generated["1-23"].applicable is False
    HostOfficer.objects.create(
        host_organization=contract.host_organization,
        name="佐藤 花子",
        position="監査役",
        not_involved_in_acceptance=True,
    )
    overview = application_sets.overview(contract.worker, "coe", BASIS)
    row = next(s for s in overview.rows if s.row["how"].get("generate") == "1-23")
    assert row.applicable is True


def test_hiring_explanation_screen(loaded: None) -> None:
    contract = make_case()
    worker = contract.worker
    client = verified_client(make_user("kanri-1-16", roles=("admin",)))
    url = f"/workers/{worker.pk}/contracts/{contract.pk}/documents/1-16/"
    page = client.get(url)
    assert page.status_code == 200
    data = {
        "basis_on": BASIS.isoformat(),
        "author": "代表取締役　山田 太郎",
        "placement": "no",
        "agency": "no",
        "guidance": "yes",
        "action": "check",
    }
    checked = client.post(url, data)
    assert checked.context["result"] is not None and not checked.context["result"].blocking
    data |= {
        "action": "generate",
        checked.context["once_field"]: checked.context["once_token"],
    }
    response = client.post(url, data)
    assert response.status_code == 302
    document = GeneratedDocument.objects.get(template__form_id="1-16")
    assert document.target == contract
