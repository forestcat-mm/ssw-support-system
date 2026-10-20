"""一括届出（電子届出の一括申込用ツール 7 本）の生成（T50。ADR-0040）。

ゲート: ダミーの届出タスク（7 種）から作った xlsm が、写した入力チェックをすべて通り、元のブックと
マクロ・ActiveX・入力規則・シートの構成が同じ。
"""

import datetime
import zipfile
from typing import Any

import openpyxl  # type: ignore[import-untyped]
import pytest

from apps.contracts.models import EmploymentContract
from apps.core.models import AuditAction, AuditLog, Organization
from apps.filings.models import Filing, FilingKind, Trigger
from apps.forms_engine.models import BulkFiling, BulkFilingFile, BulkFilingFiler
from apps.hosts.models import SupportContract
from services import bulk_filings, efile_checks, efile_data, filings, form_templates
from tests.contracts.factories import make_contract
from tests.core.factories import make_user, verified_client
from tests.forms_engine.test_form_application import make_case
from tests.hosts.factories import VALID_NUMBERS, make_host, make_site
from tests.workers.factories import add_residence, make_worker

pytestmark = pytest.mark.django_db

BASIS = datetime.date(2026, 10, 1)


@pytest.fixture
def contract(loaded: None) -> EmploymentContract:
    contract = make_case()
    add_residence(contract.worker, permitted_on=datetime.date(2025, 10, 1))
    organization = Organization.objects.get()
    organization.registration_number = "19登-000123"
    organization.corporate_number = "8010401024011"
    organization.postal_code = "100-0005"
    organization.address = "東京都千代田区丸の内1-1-1"
    organization.save()
    return contract


def task(
    kind: FilingKind,
    target: Any,
    details: dict[str, Any] | None = None,
    basis_on: datetime.date = BASIS,
) -> Filing:
    filing, _created = filings.create(
        kind, target=target, basis_on=basis_on, trigger=Trigger.MANUAL, details=details
    )
    return filing


def tasks(contract: EmploymentContract) -> dict[str, tuple[Filing, dict[str, str]]]:
    """7 本のツールそれぞれの届出タスクと、行で入れる値（既定値で足りないもの）。"""
    worker = contract.worker
    support = SupportContract.objects.get(host_organization=contract.host_organization)
    return {
        "efile-3-1": (
            task(
                FilingKind.CONTRACT_CHANGE,
                contract,
                {"required": [{"code": "base_wage_decreased", "item": "Ⅶ", "message": ""}]},
            ),
            {"e.change_content": "月額２０万円"},
        ),
        "efile-3-2": (
            task(
                FilingKind.SUPPORT_PLAN_CHANGE,
                worker,
                {"support_contract": support.pk, "event": "concluded", "plan_change_item": "III-6"},
            ),
            {},
        ),
        "efile-3-3": (
            task(
                FilingKind.SUPPORT_CONTRACT_END_OR_CONCLUSION,
                worker,
                {
                    "support_contract": support.pk,
                    "event": "terminated",
                    "termination_category": "host",
                    "termination_reason": "business",
                },
            ),
            {},
        ),
        "efile-3-4": (task(FilingKind.MISSING, worker), {"e.summary": "連絡が取れない"}),
        "efile-3-5": (task(FilingKind.STANDARD_NONCONFORMITY, worker), {}),
        "efile-3-7": (
            task(FilingKind.SUPPORT_PLAN_DIFFICULTY, worker),
            {
                "e.reason": "支援の実施困難",
                "e.category": "定期的な面談の実施",
                "e.summary": "面談に応じない",
                "e.measure": "実施再開（再開見込み含む）",
            },
        ),
        "efile-4-3": (
            task(FilingKind.SUPPORT_DIFFICULTY, worker),
            {
                "e.reason": "その他の特異事案",
                "e.category": "特定技能外国人本人の職業生活上の問題を把握",
                "e.summary": "残業代の未払いの相談",
                "e.measure": "問題解決（解決見込み含む）",
            },
        ),
    }


def _read_back(record: BulkFilingFile, table: dict[str, Any], count: int) -> list[dict[str, str]]:
    book = openpyxl.load_workbook(record.file.path, keep_vba=True)
    sheet = book[table["sheet"]]
    letters = {part for column in table["columns"] for part in column["column"].split(",")} | set(
        table["lists"]
    )
    rows = []
    for number in range(table["first_row"], table["first_row"] + count):
        row = {}
        for letter in letters:
            value = sheet[f"{letter}{number}"].value
            if value not in (None, ""):
                row[letter] = str(value)
        rows.append(row)
    return rows


@pytest.mark.parametrize(
    "form_id",
    ["efile-3-1", "efile-3-2", "efile-3-3", "efile-3-4", "efile-3-5", "efile-3-7", "efile-4-3"],
)
def test_filing_tasks_pass_the_copied_input_check(
    contract: EmploymentContract, form_id: str
) -> None:
    filing, inputs = tasks(contract)[form_id]
    assert efile_data.form_of(filing) == form_id
    bulk = bulk_filings.open_bulk_for(filing, by=None)
    assert bulk.form_id == form_id
    [row] = bulk_filings.add_filing(bulk, filing, by=None)
    bulk_filings.save_inputs(row, {**row.inputs, **inputs}, by=None)
    problems = bulk_filings.check(bulk)
    assert [(p.label, p.message) for p in problems if p.blocking] == []
    [record] = bulk_filings.generate(bulk, by=None)
    template = bulk_filings.template_of(bulk)
    source = form_templates.templates_root() / template.original_path
    table = template.mapping.definition["columns_table"]
    with zipfile.ZipFile(source) as before, zipfile.ZipFile(record.file.path) as after:
        assert before.namelist() == after.namelist()
        for name in before.namelist():
            if name != "xl/worksheets/sheet1.xml":
                assert before.read(name) == after.read(name), name
    # 書いたブックを読み戻して、写した入力チェックをもう一度通す
    rows = _read_back(record, table, 1)
    assert [
        f for f in efile_checks.check_rows(table, rows, datetime.date(2026, 10, 21)) if f.blocking
    ] == []
    book = openpyxl.load_workbook(record.file.path, keep_vba=True)
    cells = table["organization_cells"]
    name = book[table["sheet"]][cells["name"]].value
    if form_id == "efile-4-3":
        assert bulk.filer == BulkFilingFiler.ORGANIZATION and name == "テスト登録支援機関"
    else:
        assert name == contract.host_organization.name
    assert str(book[table["sheet"]][cells["address"]].value).startswith("〒")
    assert AuditLog.objects.filter(action=AuditAction.GENERATE, object_id=str(bulk.pk)).exists()


def test_defaults_come_from_the_filing_task(contract: EmploymentContract) -> None:
    every = tasks(contract)
    table = bulk_filings.template_of(
        bulk_filings.create("efile-3-1", contract.host_organization, by=None)
    ).mapping.definition["columns_table"]
    [change] = efile_data.defaults(every["efile-3-1"][0], "efile-3-1", table)
    assert (change["e.reason"], change["e.change_major"], change["e.change_minor"]) == (
        "特定技能雇用契約の変更 ",
        "賃金",
        "19.基本賃金",
    )
    termination = task(
        FilingKind.CONTRACT_TERMINATION,
        contract,
        {"termination_reason": "voluntary", "termination_note": "帰国のため"},
    )
    [ended] = efile_data.defaults(termination, "efile-3-1", table)
    assert (ended["e.end_reason"], ended["e.end_detail"], ended["e.end_other"]) == (
        "特定技能外国人の都合",
        "10.自己都合退職",
        "帰国のため",
    )
    # 変更事項ごとに 1 行（T50-D10）
    two = task(
        FilingKind.CONTRACT_CHANGE,
        contract,
        {
            "required": [
                {"code": "base_wage_decreased", "item": "Ⅶ"},
                {"code": "scheduled_hours_changed", "item": "Ⅳ"},
            ]
        },
        BASIS + datetime.timedelta(days=1),
    )
    assert len(efile_data.defaults(two, "efile-3-1", table)) == 2
    [plan] = efile_data.defaults(every["efile-3-2"][0], "efile-3-2", table)
    assert plan[efile_data.ESTIMATED] == ["e.change_major", "e.change_minor"]


def test_rows_split_by_the_numbered_rows(
    contract: EmploymentContract, monkeypatch: pytest.MonkeyPatch
) -> None:
    filing, inputs = tasks(contract)["efile-3-4"]
    bulk = bulk_filings.open_bulk_for(filing, by=None)
    [row] = bulk_filings.add_filing(bulk, filing, by=None)
    bulk_filings.save_inputs(row, {**row.inputs, **inputs}, by=None)
    second = task(FilingKind.ACCEPTANCE_DIFFICULTY, contract)
    [other] = bulk_filings.add_filing(bulk, second, by=None)
    bulk_filings.save_inputs(
        other,
        {**other.inputs, "e.reason": "特定技能所属機関の都合", "e.category": "経営上の都合"},
        by=None,
    )
    template = bulk_filings.template_of(bulk)
    monkeypatch.setitem(template.mapping.definition["columns_table"], "rows", 1)
    monkeypatch.setattr(bulk_filings, "template_of", lambda bulk, on=None: template)
    records = bulk_filings.generate(bulk, by=None)
    assert [(r.part, r.parts) for r in records] == [(1, 2), (2, 2)]
    assert bulk_filings.filename(bulk, 1, 2, datetime.date(2026, 10, 21)) == (
        f"一括届出_第3-4号_20261021_{bulk.pk}_1of2.xlsm"
    )


def test_tasks_of_another_host_or_form_are_not_added(contract: EmploymentContract) -> None:
    filing, _inputs = tasks(contract)["efile-3-4"]
    host = make_host(name="別の工業株式会社", corporate_number=VALID_NUMBERS[1])
    other = make_contract(make_worker(name_romaji="LI MING"), site=make_site(host))
    add_residence(other.worker, card_number="CD12345678EF")
    foreign = task(FilingKind.MISSING, other.worker)
    bulk = bulk_filings.open_bulk_for(filing, by=None)
    assert bulk_filings.problems_to_add(bulk, foreign)
    assert bulk_filings.problems_to_add(bulk, task(FilingKind.STANDARD_NONCONFORMITY, other.worker))
    assert foreign not in bulk_filings.candidates(bulk)
    assert filing in bulk_filings.candidates(bulk)
    assert bulk_filings.open_bulk_for(foreign, by=None).pk != bulk.pk


def test_download_needs_every_worker(contract: EmploymentContract) -> None:
    filing, inputs = tasks(contract)["efile-3-5"]
    bulk = bulk_filings.open_bulk_for(filing, by=None)
    [row] = bulk_filings.add_filing(bulk, filing, by=None)
    [record] = bulk_filings.generate(bulk, by=None)
    admin = make_user("kanri-efile", roles=("admin",))
    assert bulk_filings.can_download(admin, record)
    nobody = make_user("nobody-efile")
    assert not bulk_filings.can_download(nobody, record)


def test_screens(contract: EmploymentContract) -> None:
    filing, inputs = tasks(contract)["efile-3-4"]
    client = verified_client(make_user("kanri-efile2", roles=("admin",)))
    form = client.get("/forms/efile/new/")
    response = client.post(
        "/forms/efile/new/",
        {
            "form_id": "efile-3-4",
            "host": contract.host_organization.pk,
            form.context["once_field"]: form.context["once_token"],
        },
    )
    assert response.status_code == 302
    bulk = BulkFiling.objects.get()
    detail = client.get(f"/forms/efile/{bulk.pk}/")
    assert f'value="{filing.pk}"' in detail.content.decode()
    client.post(
        f"/forms/efile/{bulk.pk}/filings/",
        {"filings": [filing.pk], detail.context["once_field"]: detail.context["once_token"]},
    )
    row = bulk.rows.get()
    page = client.get(f"/forms/efile/{bulk.pk}/rows/{row.pk}/")
    assert page.status_code == 200
    form_fields = page.context["form"]
    data = {
        name: (value.isoformat() if isinstance(value, datetime.date) else value)
        for name, value in form_fields.initial.items()
    }
    summary = next(name for name, key in form_fields.keys.items() if key == "e.summary")
    data[summary] = "連絡が取れない"
    data[page.context["once_field"]] = page.context["once_token"]
    assert client.post(f"/forms/efile/{bulk.pk}/rows/{row.pk}/", data).status_code == 302
    row.refresh_from_db()
    assert row.inputs["e.summary"] == "連絡が取れない"
    detail = client.get(f"/forms/efile/{bulk.pk}/")
    assert 'data-problems="0"' in detail.content.decode()
    client.post(
        f"/forms/efile/{bulk.pk}/",
        {detail.context["once_field"]: detail.context["once_token"]},
    )
    record = BulkFilingFile.objects.get(bulk=bulk)
    download = client.get(f"/forms/efile/files/{record.pk}/download/")
    assert download.status_code == 200
    assert b"".join(download.streaming_content)[:2] == b"PK"  # type: ignore[attr-defined]
    assert AuditLog.objects.filter(action=AuditAction.DOWNLOAD, object_id=str(bulk.pk)).exists()
    assert "/forms/efile/" in client.get("/forms/efile/").content.decode()
