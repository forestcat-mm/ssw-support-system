"""定期届出の第3-6号・別紙1・別紙2（T51。ADR-0041）。

ゲート: ダミーデータで作った別紙1（事業所ごと）が、T24 の元データ
（periodic_report.support_rows）と一致する。
"""

import datetime
from typing import Any

import openpyxl  # type: ignore[import-untyped]
import pytest

from apps.contracts.models import EmploymentContract
from apps.core.models import AuditAction, AuditLog, Organization
from apps.forms_engine.models import GeneratedDocument, GenerationStatus
from apps.hosts.models import SupportContract
from apps.workers.models import ResidenceRecord
from services import form_data_periodic, periodic_report, staff_periods
from tests.contracts.factories import make_contract
from tests.core.factories import make_staff, make_user, verified_client
from tests.forms_engine.test_form_application import make_case
from tests.workers.factories import add_residence, make_worker

pytestmark = pytest.mark.django_db

YEAR = 2026  # 2026-04-01〜2027-03-31


@pytest.fixture
def contract(loaded: None) -> EmploymentContract:
    contract = make_case()
    SupportContract.objects.filter(host_organization=contract.host_organization).update(
        ends_on=datetime.date(2027, 3, 31)
    )
    add_residence(contract.worker, permitted_on=datetime.date(2026, 4, 1))
    staff = make_staff(name="佐藤 花子")
    office = staff.office
    office.phone_number = "03-0000-3333"
    office.save()
    staff_periods.change_worker_staff(
        contract.worker,
        staff,
        starts_on=datetime.date(2026, 4, 1),
        reason="担当",
        by=None,
        today=datetime.date(2026, 4, 1),
    )
    return contract


def _book(document: GeneratedDocument) -> Any:
    assert document.status == GenerationStatus.COMPLETED, document.failure_reason
    return openpyxl.load_workbook(document.filled_file.path)


def test_attachment_1_matches_the_source_data(contract: EmploymentContract) -> None:
    """別紙1 の行が、T24 の元データ（support_rows）と一致する（T51 のゲート）。"""
    host = contract.host_organization
    documents = form_data_periodic.request_all(host, YEAR, by=None)
    assert [d.template.form_id for d in documents] == ["3-6", "3-6-1", "3-6-2"]
    rows = [r for r in periodic_report.support_rows(YEAR) if r.host.pk == host.pk]
    assert len(rows) == 1
    row = rows[0]
    sheet = _book(documents[1])["参考様式第３－６号（別紙１）"]
    assert sheet["D3"].value == host.name
    assert sheet["R3"].value == row.site.name
    assert sheet["B8"].value == row.worker.name_romaji.upper()
    assert sheet["B10"].value == row.worker.nationality.name
    assert row.residence_record_id is not None
    record = ResidenceRecord.objects.get(pk=row.residence_record_id)
    assert sheet["C10"].value == record.card_number
    assert sheet["H7"].value == row.site.field.name
    assert row.staff is not None and sheet["S8"].value == row.staff.name
    assert sheet["S10"].value == row.staff_phone == "03-0000-3333"
    assert "■ 登録支援機関に全部委託" in sheet["O7"].value
    assert row.organization_name in sheet["O7"].value
    assert sheet["F8"].value == "■" and sheet["D8"].value == "□"  # 女
    # 期間中の特定技能1号の許可日（2026-04-01）を受入れ開始日として備考に書く
    assert sheet["U7"].value == "受入れ開始日\n2026年4月1日"
    assert sheet["B12"].value is None  # 2 人目は空
    assert AuditLog.objects.filter(
        action=AuditAction.GENERATE, object_id=str(documents[1].pk)
    ).exists()


def test_report_and_signature_sheet(contract: EmploymentContract) -> None:
    host = contract.host_organization
    documents = form_data_periodic.request_all(host, YEAR, by=None)
    report = _book(documents[0])["Sheet1"]
    assert report["F13"].value == "２０２６年４月１日　～　２０２７年３月３１日"
    digits = [report[f"{col}14"].value for col in "FGHIJKLNOPQRS"]
    assert "".join(digits) == host.corporate_number
    assert report["F15"].value == contract.versions.get().site.field.name
    assert "計( 1 )分野受入れ" in report["P15"].value
    assert report["F17"].value == host.name
    assert report["F24"].value == "1人" and report["Q24"].value == "0人"
    assert report["F43"].value.startswith("□ 自社支援のみ")
    assert "■ 支援の全部委託あり" in report["F43"].value
    assert report["F27"].value == "平均　　　　　日／月"  # ４は受入れ機関が記入
    signature = _book(documents[2])["Sheet1"]
    assert signature["D9"].value.startswith(f"【特定技能所属機関の氏名又は名称：{host.name}\u3000")
    organization = Organization.objects.get()
    assert signature["D17"].value.startswith(f"【登録支援機関名：{organization.name}")
    assert signature["D17"].value.endswith("\u3000】")
    assert "２０２６年度" in signature["A8"].value


def test_attachment_1_is_split_by_five_people(contract: EmploymentContract) -> None:
    site = contract.versions.get().site
    for index in range(5):
        other = make_contract(make_worker(name_romaji=f"WORKER {index}"), site=site)
        add_residence(other.worker, card_number=f"AB1234567{index}CD")
    lines = form_data_periodic.lines_of(contract.host_organization, YEAR)
    assert len(lines) == 6
    sheets = form_data_periodic.sheets_of(lines)
    assert [len(sheet_lines) for _site, sheet_lines in sheets] == [5, 1]


def test_acceptance_start_is_estimated_without_permission(contract: EmploymentContract) -> None:
    ResidenceRecord.objects.filter(worker=contract.worker).update(
        permitted_on=datetime.date(2026, 3, 1)
    )
    [line] = form_data_periodic.lines_of(contract.host_organization, YEAR)
    # 期間より前に特定技能1号の許可があれば、期間中の開始ではない
    assert line.accepted_from is None
    ResidenceRecord.objects.filter(worker=contract.worker).delete()
    [line] = form_data_periodic.lines_of(contract.host_organization, YEAR)
    assert line.accepted_from == line.row.accepted_from and line.accepted_from_estimated


def test_screen_and_filled_download(contract: EmploymentContract) -> None:
    host = contract.host_organization
    client = verified_client(make_user("kanri-periodic", roles=("admin",)))
    assert "/periodic-report/" in client.get(f"/hosts/{host.pk}/").content.decode()
    page = client.get(f"/hosts/{host.pk}/periodic-report/?year={YEAR}")
    assert f'data-worker="{contract.worker_id}"' in page.content.decode()
    response = client.post(
        f"/hosts/{host.pk}/periodic-report/",
        {
            "fiscal_year": YEAR,
            "action": "generate",
            page.context["once_field"]: page.context["once_token"],
        },
    )
    assert response.status_code == 302
    document = GeneratedDocument.objects.get(template__form_id="3-6")
    detail = client.get(f"/forms/documents/{document.pk}/").content.decode()
    assert 'id="document-filled"' in detail
    download = client.get(f"/forms/documents/{document.pk}/filled/")
    assert download.status_code == 200
    assert (
        "記入済み" in download["Content-Disposition"]
        or "filename*" in download["Content-Disposition"]
    )
    assert AuditLog.objects.filter(
        action=AuditAction.DOWNLOAD, object_id=str(document.pk), context__file="filled"
    ).exists()
    nobody = verified_client(make_user("nobody-periodic"))
    assert nobody.get(f"/forms/documents/{document.pk}/filled/").status_code == 403
