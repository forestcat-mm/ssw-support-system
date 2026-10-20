"""様式マスタの読み込みと版の選び方（T29、ADR-0018）。"""

import datetime
import json
import re
import zipfile
from pathlib import Path

import pytest
from django.conf import settings

from apps.forms_engine.models import FormMapping, FormTemplate, GeneratedDocument
from services import form_templates
from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db


def test_loading_is_idempotent_and_records_the_sources(loaded: None) -> None:
    template = FormTemplate.objects.get(form_id="1-17", language="ja")
    assert template.valid_from == datetime.date(2026, 10, 17)  # ADR-0007 の取得日
    assert template.revision_unknown
    assert template.original_sha256.startswith("e5d6e4a6")
    assert template.mapping.definition["fields"]
    report = form_templates.load_all()
    assert report.created == [] and report.updated == []
    assert sorted(report.unchanged) == [
        "1-17 2026-10-17 ja",
        "1-17 2026-10-17 zh-hans",
        "1-25 2026-10-18 ja",
        "1-26 2026-10-20 ja",
        "1-4 2026-10-18 ja",
        "1-5 2026-10-18 ja",
        "1-5 2026-10-18 ja-zh",
        "1-6 2026-10-18 ja",
        "1-6 2026-10-18 zh-hans",
        "13-1 2026-10-18 ja",
        "13-2 2026-10-18 ja",
        "3-1 2026-10-18 ja",
        "5-10 2026-10-18 ja",
        "5-4 2026-10-18 ja",
        "5-5 2026-10-18 ja",
        "5-6 2026-10-18 ja",
        "5-8 2026-10-17 ja",
        "5-8 2026-10-18 ja-zh",
        "5-9 2026-10-17 ja",
        "5-9 2026-10-18 ja-zh",
        "career-plan 2026-10-18 ja",
        "experience-certificate 2026-10-18 ja",
    ]
    assert FormTemplate.objects.count() == 22 and FormMapping.objects.count() == 22


def test_changed_original_stops_loading(repo_copy: tuple[Path, Path]) -> None:
    templates, _mappings = repo_copy
    form_templates.load_all()
    manifest_path = templates / "5-9" / "2026-10-17" / "manifest.json"
    original = templates / "5-9" / "2026-10-17" / "original.docx"
    original.write_bytes(original.read_bytes() + b"\0")
    with pytest.raises(form_templates.FormTemplateError):
        form_templates.load_all()  # manifest の SHA-256 と合わない
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["source"]["original_sha256"] = form_templates.sha256_of(original)
    manifest_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(form_templates.FormTemplateError):
        form_templates.load_all()  # 読み込み済みの版の original は変えられない


def test_mapping_changes_only_until_a_document_is_generated(
    repo_copy: tuple[Path, Path],
) -> None:
    _templates, mappings = repo_copy
    form_templates.load_all()
    path = mappings / "1-17" / "2026-10-17.json"
    definition = json.loads(path.read_text(encoding="utf-8"))
    definition["note"] = "changed"
    path.write_text(json.dumps(definition, ensure_ascii=False), encoding="utf-8")
    assert form_templates.load_all().updated == ["1-17 2026-10-17 ja"]

    template = FormTemplate.objects.get(form_id="1-17", language="ja")
    worker = make_worker()
    GeneratedDocument.objects.create(
        template=template,
        target=worker,
        worker=worker,
        basis_on=datetime.date(2026, 10, 18),
        snapshot="{}",
    )
    definition["note"] = "changed again"
    path.write_text(json.dumps(definition, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(form_templates.FormTemplateError):
        form_templates.load_all()


def test_select_template_uses_the_oldest_version_before_the_first_one(loaded: None) -> None:
    newer = FormTemplate.objects.get(form_id="1-17", language="ja")
    newer.pk = None
    newer.valid_from = datetime.date(2027, 4, 1)
    newer.revision_unknown = False
    newer.save()

    before = form_templates.select_template("1-17", datetime.date(2025, 1, 1))
    assert before.template.valid_from == datetime.date(2026, 10, 17)
    assert before.before_first_version
    current = form_templates.select_template("1-17", datetime.date(2026, 12, 1))
    assert current.template.valid_from == datetime.date(2026, 10, 17)
    assert not current.before_first_version
    later = form_templates.select_template("1-17", datetime.date(2027, 4, 1))
    assert later.template.valid_from == datetime.date(2027, 4, 1)
    with pytest.raises(form_templates.FormTemplateError):
        form_templates.select_template("9-99", datetime.date(2026, 12, 1))


def test_template_paths_point_into_the_repository(loaded: None) -> None:
    root = Path(settings.FORM_TEMPLATES_ROOT)
    for template in FormTemplate.objects.all():
        assert form_templates.template_file(template).is_file()
        assert (root / template.original_path).is_file()


def test_mapping_that_writes_a_cell_referenced_by_a_formula_stops_loading(
    repo_copy: tuple[Path, Path],
) -> None:
    """書くセルを参照する計算式があれば止める（PDF に古い計算結果が出るため。ADR-0018）。"""
    templates, _mappings = repo_copy
    folder = templates / "1-17" / "2026-10-17"
    original = folder / "original.xlsx"
    with zipfile.ZipFile(original) as source:
        entries = {item.filename: source.read(item.filename) for item in source.infolist()}
    sheet = entries["xl/worksheets/sheet1.xml"].decode("utf-8")
    row = re.search(r'<row r="1"[^>]*>.*?</row>', sheet, re.S)
    assert row is not None
    formula = '<c r="BY1"><f>O18&amp;""</f><v></v></c>'  # 印刷範囲の外でも、エラーでない式は見る
    sheet = sheet.replace(row.group(0), row.group(0)[:-6] + formula + "</row>", 1)
    entries["xl/worksheets/sheet1.xml"] = sheet.encode("utf-8")
    with zipfile.ZipFile(original, "w") as output:
        for name, data in entries.items():
            output.writestr(name, data)
    manifest_path = folder / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source"]["original_sha256"] = form_templates.sha256_of(original)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(form_templates.FormTemplateError, match="referenced by formulas"):
        form_templates.load_all()
    assert not FormTemplate.objects.filter(form_id="1-17").exists()
