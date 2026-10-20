"""第1-11-1号 特定技能所属機関概要書（T46b。ADR-0038）。

- テンプレート・マッピングは道具の出力。空で差し込むと公式の様式と同じ本文・頁数。
- 1 所属役員・2 決算状況・3 基準適合性は受入れ機関の台帳（役員・決算・申告の版）から。4・5 は
  全部委託なので空のまま。足りないものは警告（受入れ機関の詳細へ）。
- 申請の書類の一覧の第2表の行から生成する（雇用契約が対象）。
"""

import datetime
import json
import subprocess
import sys
from pathlib import Path

import pytest
from django.conf import settings

from apps.forms_engine.models import GenerationStatus
from apps.hosts.models import HostDeclaration, HostFinancialYear, HostOfficer, HostOrganization
from services import application_sets, form_data_application, form_generation
from tests.forms_engine import pdfcheck
from tests.forms_engine.test_form_application import make_case
from tests.forms_engine.test_form_contract import body_texts

pytestmark = pytest.mark.django_db

ROOT = Path(settings.FORM_TEMPLATES_ROOT)
FOLDER = ROOT / "1-11-1" / "2026-10-20"
MAPPING = Path(settings.FORM_MAPPINGS_ROOT) / "1-11-1" / "2026-10-20.json"
TOOLS = ROOT.parent.parent / "tools" / "forms"
BASIS = datetime.date(2026, 10, 20)


def test_template_and_mapping_are_reproduced_by_the_tool(tmp_path: Path) -> None:
    folder = tmp_path / "1-11-1"
    folder.mkdir()
    (folder / "original.docx").write_bytes((FOLDER / "original.docx").read_bytes())
    result = subprocess.run(
        [sys.executable, str(TOOLS / "make_application_templates.py"), "1-11-1", str(folder)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert (folder / "template.docx").read_bytes() == (FOLDER / "template.docx").read_bytes()
    assert json.loads(result.stdout) == json.loads(MAPPING.read_text("utf-8"))


def test_empty_template_is_the_official_form(tmp_path: Path) -> None:
    from services import form_docx

    out = tmp_path / "empty.docx"
    form_docx.render(FOLDER / "template.docx", out, {})
    assert body_texts(out) == body_texts(FOLDER / "original.docx")
    blank = pdfcheck.blank_pdf(FOLDER / "original.docx")
    empty = pdfcheck.blank_from_template(FOLDER / "template.docx")
    assert len(pdfcheck.pdf_text(blank)) == len(pdfcheck.pdf_text(empty))


def fill_host(host: HostOrganization) -> None:
    for index, (name, kana, position) in enumerate(
        [("山田 太郎", "やまだ たろう", "代表取締役"), ("佐藤 次郎", "さとう じろう", "取締役")]
    ):
        HostOfficer.objects.create(
            host_organization=host,
            name=name,
            name_kana=kana,
            position=position,
            appointed_on=datetime.date(2020 + index, 4, 1),
        )
    HostOfficer.objects.create(  # 退任した役員は載せない
        host_organization=host,
        name="鈴木 三郎",
        position="監査役",
        resigned_on=datetime.date(2026, 3, 31),
    )
    for year, sales in ((2026, 300_000_000), (2025, 280_000_000), (2024, 250_000_000)):
        HostFinancialYear.objects.create(
            host_organization=host,
            fiscal_year_end=datetime.date(year, 3, 31),
            sales=sales,
            ordinary_profit=-1_200_000 if year == 2024 else 10_000_000,
            net_profit=5_000_000,
            net_assets=80_000_000,
        )
    HostDeclaration.objects.create(
        host_organization=host,
        declared_on=datetime.date(2026, 10, 1),
        answers={"11": {"answer": "no", "detail": ""}},
        left_japanese_voluntary=2,
        left_japanese_involuntary=0,
        left_foreign_voluntary=1,
        left_foreign_involuntary=0,
        missing_ssw=0,
        missing_interns=0,
    )


def test_overview_is_made_from_the_host_ledger(loaded: None) -> None:
    contract = make_case()
    host = contract.host_organization
    empty = form_data_application.build("1-11-1", contract, BASIS, author="代表取締役　山田 太郎")
    result = form_generation.check("1-11-1", target=contract, data=empty, basis_on=BASIS)
    assert {f.code for f in result.warnings} >= {
        "overview_officers",
        "overview_financial",
        "overview_counts",
    }
    fill_host(host)
    data = form_data_application.build("1-11-1", contract, BASIS, author="代表取締役　山田 太郎")
    assert data["a.officer2_name"] == "佐藤 次郎" and "a.officer3_name" not in data
    assert data["a.sales_1"] == 300_000_000 and data["a.ordinary_3"] == -1_200_000
    assert data["a.missing_ssw_no_fault"] is True and data["a.missing_ssw_fault"] is False
    result = form_generation.check("1-11-1", target=contract, data=data, basis_on=BASIS)
    assert not result.blocking and not result.warnings
    document = form_generation.request(
        "1-11-1", target=contract, data=data, basis_on=BASIS, by=None
    )
    document.refresh_from_db()
    assert document.status == GenerationStatus.COMPLETED, document.failure_reason
    text = "".join(pdfcheck.pdf_text(Path(document.pdf_file.path)))
    assert "参考様式第１－１１－１号" in text
    for value in ("山田", "代表取締役", "３００，０００，０００", "－１，２００，０００"):
        assert value in text, value


def test_application_set_row_generates_the_overview(loaded: None) -> None:
    contract = make_case()
    overview = application_sets.overview(contract.worker, "chg", BASIS)
    rows = [s for s in overview.rows if s.row.get("how", {}).get("generate") == "1-11-1"]
    assert rows
    assert all(
        s.action_url.endswith(f"/contracts/{contract.pk}/documents/1-11-1/")
        for s in rows
        if s.action_url
    )
