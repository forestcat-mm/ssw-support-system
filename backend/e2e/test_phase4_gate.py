"""段階4の受け入れ確認（T55 の gate4。make gate4 のときだけ動く。通常の make e2e では飛ばす）。

seed_dummy のデータで、担当者（dummy_tanto）が画面で次を作ってダウンロードし、確かめる。管理画面
（/admin/）には 1 度も行かない。
1. 一括申請 3 種（認定 25 番・更新 1 番・変更〔転籍〕9 番）の xlsm。
2. 一括届出 7 種（雇用契約・支援計画変更・支援委託契約・受入れ困難・基準不適合・
   支援計画の実施困難・特異事案報告）の xlsm。届出タスクの画面の「一括届出に入れる」から
   作る。
ダウンロードした xlsm を、写した入力チェック（列項目TBL・業務コードTBL の全規則）で読み戻して
確かめ、元のブックと、入力シート以外のファイル（マクロ・ActiveX・入力規則・名前定義）が同じことを
確かめる。

ダミーの住所は架空の市区町村なので、一括申請のツールの市区町村の表に当たらない。gate4 では、使う
外国人・受入れ機関・事業所・自機関・事務所の住所を、表にある市区町村の住所に直してから作る
（担当者が台帳を直す操作に当たる。ADR-0045）。申請ごとの入力は services で入れ、画面で保存する。
"""

import datetime
import os
import zipfile
from io import StringIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import openpyxl  # type: ignore[import-untyped]
import pytest
from django.core.management import call_command
from django.utils import timezone
from playwright.sync_api import Page, Request, expect
from pytest_django.fixtures import Settings
from pytest_django.live_server_helper import LiveServer

from apps.core.models import Office, Organization, User
from apps.filings.models import Filing, FilingKind, FilingStatus
from apps.forms_engine.models import BulkApplication, BulkApplicationEntry, BulkFiling
from apps.seed.context import DUMMY_USERNAME
from apps.seed.models import SeedObject
from apps.workers.models import ForeignWorker, WorkerAddress
from e2e.conftest import SignIn
from services import (
    bulk_application_data,
    bulk_applications,
    bulk_checks,
    bulk_filings,
    efile_checks,
    form_templates,
)
from services import workers as worker_service
from tests.core.factories import TEST_PASSWORD
from tests.forms_engine.bulk_cases import fill_inputs

pytestmark = pytest.mark.skipif(os.environ.get("GATE4") != "1", reason="make gate4 のときだけ動く")

PROCEDURES = {"coe": 25, "extension": 1, "change": 9}
REAL_ADDRESS = "千葉県船橋市本町1-2-3"  # 表にある市区町村（番地以下はダミー）


@pytest.fixture
def seeded(settings: Settings, tmp_path: Path) -> list[ForeignWorker]:
    settings.DEBUG = True
    settings.ALLOW_DUMMY_DATA = True
    settings.STORAGES = {
        **settings.STORAGES,
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path / "media")},
        },
    }
    call_command("load_form_templates", stdout=StringIO())
    call_command("seed_dummy", "--date", timezone.localdate().isoformat(), stdout=StringIO())
    user = User.objects.get(username=DUMMY_USERNAME)
    user.set_password(TEST_PASSWORD)
    user.save(update_fields=["password"])
    ids = SeedObject.objects.filter(content_type__model="foreignworker").values("object_id")
    seeded = list(ForeignWorker.objects.filter(pk__in=ids).order_by("pk"))
    # 架空の市区町村の住所を、ツールの表にある市区町村の住所に直す（台帳を直す操作に当たる）
    organization = Organization.objects.get()
    organization.address = f"{REAL_ADDRESS} サンプルビル（ダミー）"
    organization.postal_code = "273-0005"
    organization.save()
    for office in Office.objects.all():
        office.address = f"{REAL_ADDRESS} サンプルビル（ダミー）"
        office.postal_code = "273-0005"
        office.save()
    for number in PROCEDURES.values():
        worker = seeded[number - 1]
        worker_service.add_record(
            worker,
            WorkerAddress(
                postal_code="273-0005",
                address=f"{REAL_ADDRESS} サンプル寮{number}号（ダミー）",
                effective_from=timezone.localdate() - datetime.timedelta(days=1),
            ),
            by=None,
        )
        contract = bulk_application_data.contract_of(worker, timezone.localdate())
        assert contract is not None
        host = contract.host_organization
        host.address = f"{REAL_ADDRESS} サンプル工場（ダミー）"
        host.postal_code = "273-0005"
        host.save()
        for site in host.sites.all():
            site.address = f"{REAL_ADDRESS} サンプル工場（ダミー）"
            site.postal_code = "273-0005"
            site.save()
    return seeded


@pytest.fixture
def admin_requests(page: Page) -> list[str]:
    seen: list[str] = []

    def watch(request: Request) -> None:
        if urlparse(request.url).path.startswith("/admin/"):
            seen.append(request.url)

    page.on("request", watch)
    return seen


def _same_structure(source: Path, made: Path, edited: set[str]) -> None:
    with zipfile.ZipFile(source) as before, zipfile.ZipFile(made) as after:
        assert before.namelist() == after.namelist()
        for name in before.namelist():
            if name not in edited:
                assert before.read(name) == after.read(name), name


def _download(page: Page, tmp_path: Path, selector: str) -> Path:
    with page.expect_download() as info:
        page.locator(selector).first.click()
    path = tmp_path / str(info.value.suggested_filename)
    info.value.save_as(path)
    return path


def _bulk_application(
    page: Page, base: str, procedure: str, worker: ForeignWorker, tmp_path: Path
) -> None:
    page.goto(f"{base}/forms/bulk/new/")
    page.locator("select[name=procedure]").select_option(procedure)
    page.locator("select[name=intermediary]").select_option(index=1)
    page.get_by_role("button", name="一括申請を作る").click()
    application = BulkApplication.objects.order_by("-pk").first()
    assert application is not None and application.procedure == procedure
    page.locator("#bulk-add-form summary").click()
    page.locator(f'#bulk-add-form input[value="{worker.pk}"]').check()
    page.locator("#bulk-add-form").get_by_role("button", name="追加").click()
    entry = BulkApplicationEntry.objects.get(application=application, worker=worker)
    fill_inputs(entry, procedure)
    page.locator(f'tr[data-entry="{entry.pk}"]').get_by_role("link", name="入力").click()
    contact = page.get_by_label("日本における連絡先（郵便番号）")
    if procedure == "coe":  # 認定だけ、日本における連絡先を行の入力に持つ（ADR-0039 決定 17）
        expect(contact).to_have_value(entry.inputs["contact_postal"])
    else:
        expect(contact).to_have_count(0)
    page.get_by_role("button", name="保存する").click()
    expect(page.get_by_text("保存しました")).to_be_visible()
    problems = bulk_applications.check(application)
    assert problems == [], [(p.label, p.message) for p in problems][:10]
    expect(page.locator(f'tr[data-entry="{entry.pk}"] [data-problems="0"]')).to_have_count(1)
    page.get_by_role("button", name="Excel を作る").click()
    made = _download(page, tmp_path, "#files a.attachment-list__name")
    template = bulk_applications.template_of(application)
    table = template.mapping.definition["columns_table"]
    book = openpyxl.load_workbook(made, keep_vba=True)
    person: dict[str, dict[str, str]] = {}
    for column in table["columns"]:
        value = book[column["sheet"]][f"{column['column']}{table['first_row']}"].value
        if value not in (None, ""):
            person.setdefault(column["sheet"], {})[column["column"]] = str(value)
    findings = bulk_checks.check_rows(table, [person], bulk_applications.procedure_key(application))
    assert findings == [], (procedure, findings[:5])
    source = form_templates.templates_root() / template.original_path
    with zipfile.ZipFile(made) as archive:
        sheets = {name for name in archive.namelist() if name.startswith("xl/worksheets/sheet")}
    _same_structure(source, made, sheets)


EFILE_INPUTS: dict[str, dict[str, Any]] = {
    "efile-3-1": {"e.change_content": "月額２０万円", "e.end_other": "一身上の都合（ダミー）"},
    "efile-3-4": {"e.summary": "連絡が取れない（ダミー）"},
    "efile-3-7": {
        "e.reason": "支援の実施困難",
        "e.category": "定期的な面談の実施",
        "e.summary": "面談に応じない（ダミー）",
        "e.measure": "実施再開（再開見込み含む）",
    },
    "efile-4-3": {
        "e.reason": "その他の特異事案",
        "e.category": "特定技能外国人本人の職業生活上の問題を把握",
        "e.summary": "職場の相談（ダミー）",
        "e.measure": "問題解決（解決見込み含む）",
    },
}
EFILE_KINDS = {
    "efile-3-1": FilingKind.CONTRACT_CHANGE,
    "efile-3-2": FilingKind.SUPPORT_PLAN_CHANGE,
    "efile-3-3": FilingKind.SUPPORT_CONTRACT_CHANGE,
    "efile-3-4": FilingKind.MISSING,
    "efile-3-5": FilingKind.STANDARD_NONCONFORMITY,
    "efile-3-7": FilingKind.SUPPORT_PLAN_DIFFICULTY,
    "efile-4-3": FilingKind.SUPPORT_DIFFICULTY,
}


def _bulk_filing(page: Page, base: str, form_id: str, tmp_path: Path) -> None:
    filing = (
        Filing.objects.filter(
            kind=EFILE_KINDS[form_id],
            status__in=[FilingStatus.NOT_STARTED, FilingStatus.IN_PROGRESS],
            worker__isnull=False,
        )
        .order_by("pk")
        .first()
    )
    assert filing is not None, form_id
    page.goto(f"{base}/filings/{filing.pk}/")
    page.get_by_role("button", name="一括届出に入れる").click()
    expect(page.get_by_text("届出タスクを一括届出に入れました。")).to_be_visible()
    bulk = BulkFiling.objects.filter(rows__filing=filing).get()
    for filing_row in bulk.rows.all():
        bulk_filings.save_inputs(
            filing_row, {**filing_row.inputs, **EFILE_INPUTS.get(form_id, {})}, by=None
        )
    page.reload()
    expect(page.locator("#efile-rows [data-problems]:not([data-problems='0'])")).to_have_count(0)
    page.get_by_role("button", name="Excel を作る").click()
    made = _download(page, tmp_path, "#files a.attachment-list__name")
    template = bulk_filings.template_of(bulk)
    table = template.mapping.definition["columns_table"]
    book = openpyxl.load_workbook(made, keep_vba=True)
    sheet = book[table["sheet"]]
    letters = {part for column in table["columns"] for part in column["column"].split(",")}
    letters |= set(table["lists"])
    rows = []
    for number in range(table["first_row"], table["first_row"] + bulk.rows.count()):
        row: dict[str, str] = {}
        for letter in letters:
            value = sheet[f"{letter}{number}"].value
            if value not in (None, ""):
                row[letter] = str(value)
        rows.append(row)
    findings = [f for f in efile_checks.check_rows(table, rows, timezone.localdate()) if f.blocking]
    assert findings == [], (form_id, findings[:5])
    source = form_templates.templates_root() / template.original_path
    _same_structure(source, made, {"xl/worksheets/sheet1.xml"})


def test_phase4_gate(
    page: Page,
    live_server: LiveServer,
    sign_in: SignIn,
    seeded: list[ForeignWorker],
    admin_requests: list[str],
    tmp_path: Path,
) -> None:
    page.set_viewport_size({"width": 1366, "height": 900})
    sign_in(User.objects.get(username=DUMMY_USERNAME))
    base = live_server.url
    for procedure, number in PROCEDURES.items():
        _bulk_application(page, base, procedure, seeded[number - 1], tmp_path)
    for form_id in EFILE_KINDS:
        _bulk_filing(page, base, form_id, tmp_path)
    assert BulkApplication.objects.count() == 3
    assert {bulk.form_id for bulk in BulkFiling.objects.all()} == set(EFILE_KINDS)
    assert admin_requests == []
