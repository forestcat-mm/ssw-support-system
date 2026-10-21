"""定期面談報告書（第5-5号・第5-6号。Excel）と相談記録書（第5-4号。Word）の生成（T35、ADR-0024）。

- マッピング定義（と第5-4号の template.docx）は道具の出力そのもの。印刷済みの文に差し込む欄は、
  値が空のとき original と同じ文になる。
- 面談の詳細から 2 様式をまとめて生成する。A「様式との一致」①②を満たす。
- 回答の無い記録（T35 より前）は質問の欄を書かず（印刷済みの □ のまま）、警告を出す。
- 長い自由記述は欄の幅で改行し、結合範囲の高さに収まるまで字を小さくする（T35-D11）。
- 第5-4号は受入れ機関×期間。9 件を超えると 2 枚目を別の生成物にする（T35-D9・D10）。
"""

import datetime
import json
import re
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import docx
import openpyxl  # type: ignore[import-untyped]
import pytest
from django.conf import settings
from django.test import Client

from apps.core.models import Role, UserRole
from apps.forms_engine.models import GeneratedDocument, GenerationStatus
from apps.support.models import ConsultationStatus, PeriodicInterview
from apps.workers.models import WorkerStatus
from services import (
    consultations,
    form_data_consultation,
    form_data_interview,
    form_generation,
    form_templates,
    interview_answers,
    support_interviews,
)
from services.consultations import EntryInput, ReceptionInput
from services.interview_answers import AnswerInput, AnswerSetInput
from services.support_catalog import SupportItem
from tests.contracts.factories import make_contract, site_with_categories
from tests.core.factories import make_organization, make_staff, make_user, verified_client
from tests.forms_engine import pdfcheck
from tests.hosts.factories import VALID_NUMBERS, make_host, make_site
from tests.support.test_periodic_interviews import TODAY, interview, row
from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db

ROOT = Path(settings.FORM_TEMPLATES_ROOT)
MAPPINGS = Path(settings.FORM_MAPPINGS_ROOT)
TOOLS = ROOT.parent.parent / "tools" / "forms"
VALID_FROM = "2026-10-18"
ON = datetime.date(2026, 7, 1)
SHEETS = {"5-5": "参考様式第５－５号", "5-6": "参考様式第５－６号"}
TAG = re.compile(r'\{\{ (\w+) or "([^"]*)" \}\}')


@pytest.fixture
def worker(loaded: None) -> Any:
    make_organization(name="（ダミー）サンプル登録支援機関")
    worker = make_worker(
        assigned_staff=make_staff(name="佐藤 花子", position="主任", is_support_staff=True),
        status=WorkerStatus.RESIDING,
    )
    make_contract(worker, site=site_with_categories())
    return worker


def mapping(form_id: str) -> dict[str, Any]:
    definition: dict[str, Any] = json.loads(
        (MAPPINGS / form_id / f"{VALID_FROM}.json").read_text("utf-8")
    )
    return definition


# --- マッピング定義と template ------------------------------------------------------------------


@pytest.mark.parametrize("form_id", ["5-5", "5-6"])
def test_interview_mappings_are_reproduced_by_the_tool(form_id: str) -> None:
    result = subprocess.run(
        [sys.executable, str(TOOLS / "make_interview_mappings.py"), form_id],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == (MAPPINGS / form_id / f"{VALID_FROM}.json").read_text("utf-8")


def test_consultation_template_is_reproduced_by_the_tool(tmp_path: Path) -> None:
    shutil.copyfile(ROOT / "5-4" / VALID_FROM / "original.docx", tmp_path / "original.docx")
    result = subprocess.run(
        [sys.executable, str(TOOLS / "make_consultation_template.py"), str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert (tmp_path / "template.docx").read_bytes() == (
        ROOT / "5-4" / VALID_FROM / "template.docx"
    ).read_bytes()
    assert result.stdout == (MAPPINGS / "5-4" / f"{VALID_FROM}.json").read_text("utf-8")


@pytest.mark.parametrize("form_id", ["5-5", "5-6"])
def test_printed_texts_are_the_original_when_empty(form_id: str) -> None:
    sheet = openpyxl.load_workbook(ROOT / form_id / VALID_FROM / "original.xlsx")[SHEETS[form_id]]
    templates = [item for item in mapping(form_id)["fields"] if item["format"] == "template"]
    assert len(templates) == 18 + 11  # 問題の有無 18・その他の事項・方式・役職・⑥・４③ の 7
    for item in templates:
        empty = {name: spec.get("empty", "") for name, spec in item["keys"].items()}
        # ブックの XML の改行は CRLF のまま（openpyxl は読むときに LF にそろえる）
        expected = sheet[item["target"].split("!")[1]].value
        assert item["template"].format(**empty).replace("\r\n", "\n") == expected


# --- 生成（A の①②） ------------------------------------------------------------------------------


def answered_interview(worker: Any, detail: str = "残業代が少ない。") -> PeriodicInterview:
    answers = interview_answers.all_no(ON)
    answers["2.1"] = AnswerInput(True, detail)
    data = interview(
        worker,
        self_answers=AnswerSetInput(answers=answers, notable="特になし。"),
        supervisor_answers=AnswerSetInput(
            answers=interview_answers.all_no(ON), supervisor_position="工場長"
        ),
    )
    return support_interviews.create_interview(worker, data, by=None, today=TODAY)


def written_values(form_id: str, data: dict[str, Any]) -> dict[str, object]:
    return form_generation.field_values(mapping(form_id), data)


def workbook_structure(path: Path) -> dict[str, object]:
    """制約3: ファイルの一覧・結合セル・入力規則・保護・印刷の設定・名前の定義と、書いたシートの
    ほかのファイル（styles.xml を含む。字の大きさはセルの中に持つ。修正 1 回目 A8）。"""
    with zipfile.ZipFile(path) as archive:
        sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
        workbook = archive.read("xl/workbook.xml").decode("utf-8")
        return {
            "files": archive.namelist(),
            "page_setup": re.findall(r"<pageSetup[^>]*/>", sheet),
            "margins": re.findall(r"<pageMargins[^>]*/>", sheet),
            "print_options": re.findall(r"<printOptions[^>]*/>", sheet),
            "header_footer": re.findall(r"<headerFooter.*?</headerFooter>", sheet, re.S),
            "breaks": re.findall(r"<rowBreaks.*?</rowBreaks>", sheet, re.S),
            "merges": re.findall(r"<mergeCell [^>]*/>", sheet),
            "validations": re.findall(r"<dataValidations.*?</dataValidations>", sheet, re.S),
            "protection": re.findall(r"<sheetProtection[^>]*/>", sheet),
            "defined_names": re.findall(r"<definedName .*?</definedName>", workbook),
            "others": {
                name: archive.read(name)
                for name in archive.namelist()
                if name
                != "xl/worksheets/sheet1.xml"  # styles.xml も同じ（字の大きさはセルの中に持つ）
            },
        }


def check_document(document: GeneratedDocument, form_id: str, data: dict[str, Any]) -> None:
    document.refresh_from_db()
    assert document.status == GenerationStatus.COMPLETED, document.failure_reason
    original = ROOT / form_id / VALID_FROM / "original.xlsx"
    blank = pdfcheck.blank_pdf(original)
    assert document.page_count == len(pdfcheck.pdf_text(blank)) == 2
    values = written_values(form_id, data)
    sheet = openpyxl.load_workbook(original)[SHEETS[form_id]]
    printed = [sheet[target.split("!")[1]].value for target in values]
    # ① 文字ごとの数（改行・空白は数えない）
    assert pdfcheck.fixed_characters_match(
        Path(document.pdf_file.path), blank, values.values(), printed
    )
    # ② 書いたセルを読み戻す（改行を入れた欄は空白を除いて比べる）
    filled = openpyxl.load_workbook(document.filled_file.path)[SHEETS[form_id]]
    for target, value in values.items():
        got = str(filled[target.split("!")[1]].value)
        assert "".join(got.split()) == "".join(str(value).split()), target
    # 制約3: シートの構造とほかのファイルは公式のブックと同じ（違うのは書いたセルと字の大きさだけ）
    assert workbook_structure(Path(document.filled_file.path)) == workbook_structure(original)


def test_reports_are_generated_together_from_the_interview(worker: Any) -> None:
    saved = answered_interview(worker)
    client = verified_client(make_user("tanto55", roles=("support_staff",)))
    url = f"/workers/{worker.pk}/support/interviews/{saved.pk}/generate/"
    page = client.get(url)
    assert page.status_code == 200 and "不足はありません" in page.content.decode()
    response = client.post(url, {page.context["once_field"]: page.context["once_token"]})
    assert response["Location"] == f"/workers/{worker.pk}/documents/"
    documents = {
        document.template.form_id: document
        for document in GeneratedDocument.objects.filter(worker=worker)
    }
    assert set(documents) == {"5-5", "5-6"}
    for form_id, document in documents.items():
        assert document.target == saved and document.basis_on == saved.basis_on
        assert document.template.requires_signed_copy is False  # 署名欄が無い（T35-D12）
        snapshot = json.loads(document.snapshot)
        check_document(document, form_id, snapshot["data"])
    values = written_values("5-5", json.loads(documents["5-5"].snapshot)["data"])
    assert values[f"{SHEETS['5-5']}!W21"] == "■有  □無"  # 2.1 は問題有
    assert values[f"{SHEETS['5-5']}!W18"] == "□有  ■無"
    assert values[f"{SHEETS['5-5']}!L8"] == worker.name_romaji
    supervisor = written_values("5-6", json.loads(documents["5-6"].snapshot)["data"])
    assert supervisor[f"{SHEETS['5-6']}!L8"] == "工場長 田中（工場長）"
    detail = client.get(f"/forms/documents/{documents['5-5'].pk}/").content.decode()
    assert f"/support/interviews/{saved.pk}/" in detail


def test_old_record_keeps_the_printed_boxes_and_warns(worker: Any) -> None:
    bare = support_interviews.InterviewInput(
        self_row=row(worker, ON), supervisor_row=row(worker, ON, counterpart_name="工場長 田中")
    )
    saved = support_interviews.create_interview(
        worker, bare, by=None, today=TODAY, without_answers=True
    )
    data = form_data_interview.build(saved, "5-5", made_on=TODAY)
    result = form_generation.check("5-5", target=saved, data=data, basis_on=saved.basis_on)
    assert {finding.code for finding in result.warnings} == {"interview_answers_missing"}
    values = written_values("5-5", data)
    assert not any(target.endswith(("!W18", "!F36", "!Z18")) for target in values)
    document = form_generation.request(
        "5-5", target=saved, data=data, basis_on=saved.basis_on, by=None, accept_warnings=True
    )
    check_document(document, "5-5", json.loads(document.snapshot)["data"])


def test_long_details_wrap_and_shrink_and_overflow_is_warned(worker: Any) -> None:
    saved = answered_interview(worker, detail="合意なしに居住費が変更された。" * 2)
    data = form_data_interview.build(saved, "5-5", made_on=TODAY)
    template = form_templates.select_template("5-5", ON).template
    [fit] = [f for f in form_generation.fits(template, data) if f.target.endswith("!Z21")]
    assert fit.wrap and fit.size < 9 and not fit.overflow
    document = form_generation.request("5-5", target=saved, data=data, basis_on=ON, by=None)
    document.refresh_from_db()
    cell = openpyxl.load_workbook(document.filled_file.path)[SHEETS["5-5"]]["Z21"].value
    assert "\n" in str(cell)
    other = make_worker(status=WorkerStatus.RESIDING, assigned_staff=worker.assigned_staff)
    make_contract(other, site=worker.employment_contracts.get().versions.get().site)
    long_interview = answered_interview(other, detail="長い。" * 200)
    long = form_data_interview.build(long_interview, "5-5", made_on=TODAY)
    result = form_generation.check("5-5", target=long_interview, data=long, basis_on=ON)
    assert "text_overflow" in {finding.code for finding in result.warnings}


# --- 第5-4号 相談記録書 ------------------------------------------------------------------------


def consultation(worker: Any, on: datetime.date, body: str = "寮の騒音の相談。") -> Any:
    case = consultations.create_consultation(
        worker,
        ReceptionInput(
            record=replace(
                row(worker, on), kind=SupportItem.CONSULTATION, method="phone", body=body
            ),
            category="daily_life",
        ),
        by=None,
        today=TODAY,
    )
    consultations.add_entry(
        case,
        EntryInput(
            occurred_on=on,
            method="phone",
            staff=worker.assigned_staff,
            body="管理会社に連絡した。",
            status_after=ConsultationStatus.RESOLVED,
        ),
        by=None,
        today=TODAY,
    )
    return case


def test_consultation_records_are_split_by_nine(worker: Any) -> None:
    host = worker.employment_contracts.get().host_organization
    for day in range(1, 11):
        consultation(worker, datetime.date(2026, 6, day))
    rows = form_data_consultation.consultations_for(
        host, datetime.date(2026, 4, 1), datetime.date(2027, 3, 31)
    )
    assert len(rows) == 10
    assert [len(page) for page in form_data_consultation.pages(rows)] == [9, 1]
    other_site = make_site(make_host(corporate_number=VALID_NUMBERS[1]))
    other_host = make_contract(
        make_worker(status=WorkerStatus.RESIDING), site=other_site
    ).host_organization
    assert (
        form_data_consultation.consultations_for(
            other_host, datetime.date(2026, 4, 1), datetime.date(2027, 3, 31)
        )
        == []
    )
    data = form_data_consultation.build(
        datetime.date(2026, 4, 1), datetime.date(2027, 3, 31), rows[:9]
    )
    assert data["c.r1_received_on"] == "2026/6/1" and data["c.r1_sex"] in ("男", "女")
    assert data["c.r1_result"] == "2026/6/1 管理会社に連絡した。"
    assert data["c.organization_name"] == "（ダミー）サンプル登録支援機関"


def test_consultation_record_screen_generates_documents(
    worker: Any, today_is: Callable[[datetime.date], None]
) -> None:
    today_is(TODAY)  # 画面の既定の期間（今日の年度 2026-04-01〜2027-03-31）に 6 月の相談が入る日
    host = worker.employment_contracts.get().host_organization
    for day in range(1, 11):
        consultation(worker, datetime.date(2026, 6, day))
    client = verified_client(make_user("tanto54", roles=("support_staff",)))
    detail = client.get(f"/hosts/{host.pk}/").content.decode()
    assert 'id="consultation-records"' in detail
    url = f"/hosts/{host.pk}/consultation-records/"
    page = client.get(url)
    assert (
        page.status_code == 200 and "期間の相談は 10 件です（帳票 2 枚）" in page.content.decode()
    )
    response = client.post(
        url,
        {
            page.context["once_field"]: page.context["once_token"],
            "starts_on": "2026-04-01",
            "ends_on": "2027-03-31",
            "action": "generate",
        },
    )
    assert response.status_code == 302
    documents = list(GeneratedDocument.objects.filter(template__form_id="5-4").order_by("pk"))
    assert len(documents) == 2 and all(document.worker is None for document in documents)
    first = documents[0]
    first.refresh_from_db()
    assert first.status == GenerationStatus.COMPLETED, first.failure_reason
    # ① 頁数と固定の文言（空の様式は template に何も入れずに差し込んだもの）
    blank = pdfcheck.blank_from_template(ROOT / "5-4" / VALID_FROM / "template.docx")
    assert first.page_count == len(pdfcheck.pdf_text(blank)) == 2
    data = json.loads(first.snapshot)["data"]
    values = written_values("5-4", data)
    with zipfile.ZipFile(ROOT / "5-4" / VALID_FROM / "template.docx") as archive:
        originals = dict(TAG.findall(archive.read("word/document.xml").decode()))
    printed = [originals[target] for target in values]
    assert pdfcheck.fixed_characters_match(
        Path(first.pdf_file.path), blank, [str(v) for v in values.values()], printed
    )
    # ② 本文に値がある
    text = "\n".join(
        paragraph.text
        for table in docx.Document(first.filled_file.path).tables
        for table_row in table.rows
        for cell in table_row.cells
        for paragraph in cell.paragraphs
    )
    assert worker.name_romaji in text and "2026/6/1" in text
    assert f"/hosts/{host.pk}/" in client.get(f"/forms/documents/{first.pk}/").content.decode()


def test_consultation_records_need_the_permission_to_view_workers(worker: Any) -> None:
    """複数の外国人の個人情報を含むので、帳票の生成と外国人の閲覧の両方が要る。"""
    host = worker.employment_contracts.get().host_organization
    user = make_user("forms_only")
    role = Role.objects.create(code="forms_only", name="帳票だけ")
    role.permissions.create(action="forms.generate")
    UserRole.objects.create(user=user, role=role)
    client: Client = verified_client(user)
    assert client.get(f"/hosts/{host.pk}/consultation-records/").status_code == 403


# --- ダミーデータ -----------------------------------------------------------------------------


def test_dummy_interviews_have_the_scenes_of_t35(seeded: list[Any]) -> None:
    """最後の組は回答つき（3・19 番は回答なし）。古い組は回答なし。7 番は ⑥ 有りで 1 件のタスク。"""
    from apps.filings.models import Filing, FilingKind
    from apps.seed import support_builders
    from services import incident_reports

    answered = 0
    for number, worker in enumerate(seeded, start=1):
        pairs = list(
            PeriodicInterview.objects.filter(worker=worker, voided_at__isnull=True).order_by(
                "basis_on"
            )
        )
        if not pairs:
            continue
        *older, last = pairs
        if number != support_builders.REPORTED_INTERVIEW:  # 7 番は通報の組が最後に足される
            assert not any(interview_answers.has_answers(pair) for pair in older), number
        if number in support_builders.NO_ANSWERS:
            assert not interview_answers.has_answers(last)
        else:
            assert interview_answers.has_answers(last), number
            answered += 1
    assert answered > 20
    reporter = seeded[support_builders.REPORTED_INTERVIEW - 1]
    case = PeriodicInterview.objects.get(worker=reporter, violation_suspected=True)
    assert interview_answers.nonconformity_of(case).exists  # type: ignore[union-attr]
    [task] = Filing.objects.filter(
        kind=FilingKind.SUPPORT_DIFFICULTY,
        trigger_details__sources__contains=[incident_reports.interview_key(case)],
    )
    assert task.trigger_details["sources"] == incident_reports.interview_keys(case)
    online = seeded[support_builders.ONLINE_INTERVIEW - 1]
    record = (
        PeriodicInterview.objects.filter(worker=online)
        .order_by("-basis_on")
        .first()
        .records.get(counterpart="worker")  # type: ignore[union-attr]
    )
    assert record.method == "online"
    assert interview_answers.answers_of(record).recording_location  # type: ignore[union-attr]
