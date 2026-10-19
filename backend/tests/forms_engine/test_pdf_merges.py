"""PDF に変換する一時の写しでだけ結合する範囲（修正 1 回目 A6 の案 (e)、ADR-0032）。

- 保存する記入済みのブックは結合しない（公式の構造のまま）。変換の直前の写しでだけ結合する。
- 結合してよい範囲は manifest の pdf_merges に一覧で持ち、範囲の中に罫線・値・ほかの欄・結合が
  無いことを読み込みのときに確かめる。範囲の左上の欄は字を小さくせず、収まらないときだけ知らせる。
"""

import json
import zipfile
from pathlib import Path
from typing import Any

import openpyxl  # type: ignore[import-untyped]
import pytest
from openpyxl.styles import Border, Side  # type: ignore[import-untyped]

from apps.forms_engine.models import FormTemplate, GenerationStatus
from apps.workers.models import ForeignWorker
from services import form_convert, form_generation, form_samples, form_templates, form_xlsx
from tests.forms_engine.conftest import BASE

pytestmark = pytest.mark.django_db

SHEET = "参考様式第１－１７号"


def small_book(path: Path) -> Path:
    book = openpyxl.Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "S"
    sheet["C3"] = "印刷済み"
    sheet["B6"].border = Border(bottom=Side(style="thin"))
    sheet.merge_cells("E1:F1")
    book.save(path)
    return path


def test_merge_ranges_are_checked(tmp_path: Path) -> None:
    path = small_book(tmp_path / "small.xlsx")
    targets = ["S!A1", "S!A5", "S!A8", "S!D1", "S!A3"]
    assert form_xlsx.pdf_merge_errors(path, ["S!A1:B2"], targets) == []
    assert form_xlsx.pdf_merge_errors(path, ["S!B1:B2"], targets) == [
        "S!B1:B2: the top-left cell is not a target of the mapping"
    ]
    assert "C3 has a value" in form_xlsx.pdf_merge_errors(path, ["S!A3:C4"], targets)[0]
    assert (
        "B6 has a border inside (bottom)"
        in form_xlsx.pdf_merge_errors(path, ["S!A5:B7"], targets)[0]
    )
    assert form_xlsx.pdf_merge_errors(path, ["S!A6:B6"], targets)[0].endswith(
        "not a target of the mapping"
    )
    assert "overlaps a merged range" in form_xlsx.pdf_merge_errors(path, ["S!D1:E2"], targets)[0]
    assert "A8 is another target" in form_xlsx.pdf_merge_errors(path, ["S!A5:A8"], targets)[-1]
    # 外周の罫線（範囲の最後の行の下の線）は結合してよい
    assert form_xlsx.pdf_merge_errors(path, ["S!B5:B6"], ["S!B5"]) == []


def test_loader_rejects_a_bad_merge_range(repo_copy: tuple[Path, Path]) -> None:
    templates, _mappings = repo_copy
    manifest = templates / "1-17" / "2026-10-17" / "manifest.json"
    data = json.loads(manifest.read_text("utf-8"))
    data["pdf_merges"] = [{"range": f"{SHEET}!O42:BA49", "field": "〒の行を含む（誤り）"}]
    manifest.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    with pytest.raises(form_templates.FormTemplateError, match="pdf_merges"):
        form_templates.load_all()


def merges_of(path: Path) -> list[tuple[int, int, int, int]]:
    with zipfile.ZipFile(path) as archive:
        sheet = form_xlsx.sheet_paths(archive)[SHEET]
        return form_xlsx.merged_ranges(archive.read(sheet).decode("utf-8"))


def test_only_the_copy_for_the_pdf_is_merged(
    seeded: list[ForeignWorker], monkeypatch: pytest.MonkeyPatch
) -> None:
    template = form_templates.select_template("1-17", BASE).template
    assert form_generation.pdf_merges(template) == [f"{SHEET}!O44:BA49", f"{SHEET}!AO371:BB381"]
    converted: list[list[tuple[int, int, int, int]]] = []
    real_convert = form_convert.convert

    def watch(path: Path) -> Path:
        converted.append(merges_of(path))
        return real_convert(path)

    monkeypatch.setattr(form_convert, "convert", watch)
    target, data = form_samples.sample("1-17", seeded[14], BASE)
    document = form_generation.request(
        "1-17", target=target, data=data, basis_on=BASE, by=None, accept_warnings=True
    )
    document.refresh_from_db()
    assert document.status == GenerationStatus.COMPLETED, document.failure_reason
    original = form_templates.template_file(template)
    saved = merges_of(Path(document.filled_file.path))
    assert saved == merges_of(original)  # 保存するブックは公式の構造のまま
    address = (form_xlsx.column_index("O"), 44, form_xlsx.column_index("BA"), 49)
    assert address in converted[0] and address not in saved
    assert len(converted[0]) == len(saved) + 2
    snapshot: dict[str, Any] = json.loads(document.snapshot)
    assert f"{SHEET}!O44" not in snapshot["fitted"]  # 13pt のまま（字を小さくしない）


def test_long_address_is_warned_not_shrunk(loaded: None) -> None:
    template = FormTemplate.objects.get(form_id="1-17", language="ja")
    long_address = "東京都サンプル区ダミー町" + "１丁目２番３号サンプルビル" * 5
    fits = {
        fit.target: fit for fit in form_generation.fits(template, {"host.address": long_address})
    }
    fit = fits[f"{SHEET}!O44"]
    assert fit.size == 13 and fit.overflow  # 生成前の検査の「欄に収まりません」の警告
    short = form_generation.fits(template, {"host.address": "静岡県ダミー市サンプル町7-8-9"})
    assert f"{SHEET}!O44" not in {f.target for f in short}
