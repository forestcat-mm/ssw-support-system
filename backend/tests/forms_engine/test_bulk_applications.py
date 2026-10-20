"""一括申請の生成（認定・変更・更新。T48）。

ゲート: ダミーの外国人で作った xlsm が、写した入力チェックをすべて通り、元のブックとマクロ・
保護・入力規則・シートの構成が同じ。
"""

import json
import zipfile

import openpyxl  # type: ignore[import-untyped]
import pytest

from apps.core.models import AuditAction, AuditLog
from apps.forms_engine.models import BulkApplicationFile
from services import bulk_application_data, bulk_applications, bulk_checks, form_templates
from tests.core.factories import make_user, verified_client
from tests.forms_engine.bulk_cases import complete_application

pytestmark = pytest.mark.django_db

PROCEDURES = ["coe", "extension", "change"]


@pytest.mark.parametrize("procedure", PROCEDURES)
def test_complete_workers_pass_the_copied_input_check(loaded: None, procedure: str) -> None:
    application = complete_application(procedure)
    problems = bulk_applications.check(application)
    assert [(p.entry.worker.name_romaji, p.label, p.message) for p in problems] == []
    [record] = bulk_applications.generate(application, by=None)
    assert record.worker_ids == [e.worker_id for e in application.entries.order_by("pk")]
    template = bulk_applications.template_of(application)
    source = form_templates.templates_root() / template.original_path
    with zipfile.ZipFile(source) as before, zipfile.ZipFile(record.file.path) as after:
        assert before.namelist() == after.namelist()
        for name in ("xl/vbaProject.bin", "xl/workbook.xml", "xl/styles.xml"):
            assert before.read(name) == after.read(name), name
    # 書いたブックを読み戻して、写した入力チェックをもう一度通す
    table = template.mapping.definition["columns_table"]
    book = openpyxl.load_workbook(record.file.path, keep_vba=True)
    people = []
    for row in range(table["first_row"], table["first_row"] + 2):
        person: dict[str, dict[str, str]] = {}
        for column in table["columns"]:
            value = book[column["sheet"]][f"{column['column']}{row}"].value
            if value not in (None, ""):
                person.setdefault(column["sheet"], {})[column["column"]] = str(value)
        people.append(person)
    findings = bulk_checks.check_rows(table, people, bulk_applications.procedure_key(application))
    assert findings == []
    common = book[table["common_sheet"]]
    assert common["A8"].value is not None  # 行の番号の数式
    assert book["メニュー"]["D15"].value == "特定技能 Specified Skilled Worker"
    assert AuditLog.objects.filter(
        action=AuditAction.GENERATE, object_id=str(application.pk)
    ).exists()


def test_files_are_split_by_300_people(loaded: None, monkeypatch: pytest.MonkeyPatch) -> None:
    application = complete_application("extension")
    monkeypatch.setattr(bulk_applications, "PEOPLE_PER_FILE", 1)
    records = bulk_applications.generate(application, by=None)
    assert [(r.part, r.parts) for r in sorted(records, key=lambda r: r.part)] == [(1, 2), (2, 2)]
    assert "1of2" in bulk_applications.filename(application, 1, 2)


def test_missing_ledger_items_block_generation(loaded: None) -> None:
    application = complete_application("extension", people=1)
    entry = application.entries.get()
    entry.worker.birthplace = ""
    entry.worker.save()
    bulk_applications.save_inputs(entry, {}, sensitive=None, by=None)
    labels = {p.label for p in bulk_applications.check(application)}
    assert "配偶者の有無" in labels and "職業" in labels
    with pytest.raises(bulk_applications.GenerationBlocked):
        bulk_applications.generate(application, by=None)


def test_criminal_record_is_restricted(loaded: None) -> None:
    application = complete_application("extension", people=1)
    entry = application.entries.get()
    bulk_applications.save_inputs(
        entry,
        entry.inputs,
        sensitive={"crime": "yes", "crime_detail": "道路交通法違反（罰金）"},
        by=None,
    )
    log = AuditLog.objects.filter(object_id=str(entry.pk), changes__has_key="sensitive").latest(
        "pk"
    )
    assert "道路交通法" not in json.dumps(log.changes, ensure_ascii=False)
    [record] = bulk_applications.generate(application, by=None)
    assert record.includes_sensitive
    assert "道路交通法" not in record.snapshot
    staff_user = make_user("tanto-other", roles=("support_staff",))
    assert not bulk_applications.can_download(staff_user, record)
    assigned = make_user("tanto-assigned", roles=("support_staff",))
    staff = entry.worker.assigned_staff
    staff.user = assigned
    staff.save()
    entry.worker.refresh_from_db()
    assert bulk_applications.can_download(assigned, record)
    admin = make_user("kanri-bulk", roles=("admin",))
    assert bulk_applications.can_download(admin, record)
    # 担当でない人には入力の画面に犯罪の欄を出さない
    client = verified_client(staff_user)
    page = client.get(f"/forms/bulk/{application.pk}/entries/{entry.pk}/")
    assert 'id="bulk-sensitive-hidden"' in page.content.decode()
    assert "道路交通法" not in page.content.decode()
    assert client.get(f"/forms/bulk/files/{record.pk}/download/").status_code == 403


def test_screens(loaded: None) -> None:
    application = complete_application("change", people=1)
    client = verified_client(make_user("kanri-bulk2", roles=("admin",)))
    page = client.get("/forms/bulk/")
    assert f'data-bulk="{application.pk}"' in page.content.decode()
    detail = client.get(f"/forms/bulk/{application.pk}/")
    assert 'data-problems="0"' in detail.content.decode()
    response = client.post(
        f"/forms/bulk/{application.pk}/",
        {detail.context["once_field"]: detail.context["once_token"]},
    )
    assert response.status_code == 302
    record = BulkApplicationFile.objects.get(application=application)
    download = client.get(f"/forms/bulk/files/{record.pk}/download/")
    assert download.status_code == 200
    assert b"".join(download.streaming_content)[:2] == b"PK"  # type: ignore[attr-defined]
    assert AuditLog.objects.filter(
        action=AuditAction.DOWNLOAD, object_id=str(application.pk)
    ).exists()


def test_inputs_default_to_the_previous_entry(loaded: None) -> None:
    application = complete_application("extension", people=1)
    entry = application.entries.get()
    defaults = bulk_application_data.default_inputs(entry.worker)
    assert defaults["occupation"] == "会社員"
