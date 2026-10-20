"""第1-26号 公的義務履行に関する誓約書（T42 の追加）。

- テンプレート・マッピングは道具の出力。空で差し込むと公式の様式と同じ本文・頁数。
- 理由（納付緩和措置の申請日、またはやむを得ない理由の文）が無ければ生成不可。署名は手で書く。
- 申請の書類の一覧では、滞納があるかをシステムで判断しない（当てはまるかを確かめる行。
  一括生成の対象外）。
"""

import datetime
import json
import subprocess
import sys
from pathlib import Path

import pytest
from django.conf import settings

from apps.forms_engine.models import GeneratedDocument, GenerationStatus
from services import application_bulk, application_sets, form_data_application, form_generation
from tests.core.factories import make_user, verified_client
from tests.forms_engine import pdfcheck
from tests.forms_engine.test_form_application import make_case
from tests.forms_engine.test_form_contract import body_texts

pytestmark = pytest.mark.django_db

ROOT = Path(settings.FORM_TEMPLATES_ROOT)
FOLDER = ROOT / "1-26" / "2026-10-20"
MAPPING = Path(settings.FORM_MAPPINGS_ROOT) / "1-26" / "2026-10-20.json"
TOOLS = ROOT.parent.parent / "tools" / "forms"
BASIS = datetime.date(2026, 10, 20)


def test_template_and_mapping_are_reproduced_by_the_tool(tmp_path: Path) -> None:
    folder = tmp_path / "1-26"
    folder.mkdir()
    (folder / "original.docx").write_bytes((FOLDER / "original.docx").read_bytes())
    result = subprocess.run(
        [sys.executable, str(TOOLS / "make_application_templates.py"), "1-26", str(folder)],
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


def test_reason_is_required_and_the_form_is_generated(loaded: None) -> None:
    contract = make_case()
    worker = contract.worker
    empty = form_data_application.build_1_26(BASIS, relief_on=None, other_reason="")
    result = form_generation.check("1-26", target=worker, data=empty, basis_on=BASIS)
    assert [f.code for f in result.blocking] == ["public_duty_reason_missing"]
    data = form_data_application.build_1_26(
        BASIS, relief_on=datetime.date(2026, 10, 1), other_reason=""
    )
    document = form_generation.request("1-26", target=worker, data=data, basis_on=BASIS, by=None)
    document.refresh_from_db()
    assert document.status == GenerationStatus.COMPLETED, document.failure_reason
    assert document.template.requires_signed_copy
    text = "".join(pdfcheck.pdf_text(Path(document.pdf_file.path)))
    assert "２０２６" in text and "公的義務履行に関する誓約書" in text


def test_row_is_not_judged_and_not_in_bulk(loaded: None) -> None:
    contract = make_case()
    overview = application_sets.overview(contract.worker, "chg", BASIS)
    state = next(s for s in overview.rows if s.row["id"] == "chg_t1-17-1")
    assert state.applicable is None  # 滞納はシステムでは判断しない
    assert state.status == application_sets.Status.NOT_GENERATED
    assert state.action_url.endswith(f"/workers/{contract.worker_id}/documents/public-duty-pledge/")
    assert "chg_t1-17-1" not in {c.row_id for c in application_bulk.candidates(overview)}


def test_screen_generates_with_another_reason(loaded: None) -> None:
    contract = make_case()
    client = verified_client(make_user("tanto126", roles=("support_staff",)))
    url = f"/workers/{contract.worker_id}/documents/public-duty-pledge/"
    page = client.get(url)
    assert page.status_code == 200
    data = {
        page.context["once_field"]: page.context["once_token"],
        "basis_on": "2026-10-20",
        "reason": "other",
        "other_reason": "勤務先の倒産で収入が無かったため",
        "action": "generate",
    }
    assert client.post(url, data).status_code == 302
    document = GeneratedDocument.objects.get(template__form_id="1-26")
    assert (
        json.loads(document.snapshot)["data"]["a.other_reason"]
        == "勤務先の倒産で収入が無かったため"
    )
