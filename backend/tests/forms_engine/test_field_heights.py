"""書いた値が欄（行・結合範囲）の高さに収まるか（修正 1 回目 A6）。

LibreOffice は行の高さからはみ出した字を切る（Excel は下の空の行に描く）。第1-17号 Ⅱ欄２の住所
（O44。8.25pt の 1 行に 13pt）がそうだった。この欄は、PDF に変換する一時の写しでだけ住所の場所を
結合する（manifest の pdf_merges。修正 1 回目 A6 の案 (e)、ADR-0032）ので、結合した範囲の高さで
確かめる。
- Excel の様式（第1-17号・5-5号・5-6号）: マッピングのすべての欄について、書く字の大きさ（字を小さく
  した欄はその大きさ）× 行間 が高さに収まる。値はダミーの全員の値と、各欄の最長の値（200 字）。
- Word の様式: 差込の位置が高さ固定の行（w:hRule="exact"）に無い（固定でない行は伸びるので
  切れない）。
"""

import datetime
import re
import zipfile
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings

from apps.forms_engine.models import FormMethod, FormTemplate
from apps.workers.models import ForeignWorker
from services import form_generation, form_samples, form_templates, form_xlsx
from tests.forms_engine.conftest import BASE

pytestmark = pytest.mark.django_db

ROOT = Path(settings.FORM_TEMPLATES_ROOT)
MERGED_FOR_PDF = {"参考様式第１－１７号!O44"}
# いつも空の欄: Ⅳ４ の委託先の住所（結合されていない 1 行）。委託が有の行は全部委託の計画では
# 生成不可（plan_not_outsourced）で、委託が無ければ空を書く。最長の値の確かめから外す（ADR-0032）
ALWAYS_EMPTY = {"参考様式第１－１７号!AO371"}
LONG = "長" * 200


def longest(fmt: str) -> Any:
    """書式ごとの最長の値（文字は 200 字、日付は 2 桁の月日、数は 7 桁）。"""
    if fmt in ("date", "year", "month", "day"):
        return datetime.date(2026, 12, 28)
    if fmt in ("number", "yen", "zen", "zen_2", "zen_yen"):
        return 1234567
    if fmt == "check":
        return True
    return LONG


def text_targets(template: FormTemplate) -> list[dict[str, Any]]:
    return [
        item
        for item in template.mapping.definition["fields"]
        if "target" in item and item.get("format") != "check"
    ]


def written_sizes(
    template: FormTemplate, data: dict[str, Any]
) -> dict[str, tuple[float, float, int]]:
    """書き込み先 → (字の大きさ, 高さ, 行数)。"""
    values = form_generation.field_values(template.mapping.definition, data)
    items = {item["target"]: item for item in text_targets(template)}
    targets = [t for t in values if t in items and values[t] not in (None, "")]
    areas = form_generation.text_areas(template)  # fit_area と一時の結合の範囲
    fonts = form_xlsx.cell_fonts(form_templates.template_file(template), targets, areas)
    fitted = {fit.target: fit for fit in form_generation.fits(template, data)}
    result = {}
    for target in targets:
        if target in fitted and fitted[target].overflow:
            continue  # 「欄に収まりません」の警告（text_fits）が出る
        font = fonts[target]
        size = fitted[target].size if target in fitted else font.size
        lines = (
            len(form_xlsx.wrap_lines(str(values[target]), font, size))
            if items[target].get("wrap")
            else 1
        )
        result[target] = (size, font.height_pt, lines)
    return result


def too_tall(sizes: dict[str, tuple[float, float, int]]) -> list[str]:
    return sorted(
        target
        for target, (size, height, lines) in sizes.items()
        if lines * size * form_xlsx.LINE_SPACING > height + 0.01 and target not in ALWAYS_EMPTY
    )


def test_form_1_17_values_of_every_dummy_worker_fit_the_row_height(
    seeded: list[ForeignWorker],
) -> None:
    template = form_templates.select_template("1-17", BASE).template
    checked = 0
    for worker in seeded:
        try:
            _target, data = form_samples.sample("1-17", worker, BASE)
        except ValueError:  # 計画などが無い人
            continue
        sizes = written_sizes(template, data)
        assert too_tall(sizes) == [], worker
        checked += 1
    assert checked >= 25


@pytest.mark.parametrize("form_id", ["1-17", "5-5", "5-6"])
def test_longest_values_fit_the_row_height(loaded: None, form_id: str) -> None:
    """各欄に最長の値を入れても、字を小さくする欄は高さに収まる。小さくしない欄は、元の字の大きさが
    高さに収まる（値の長さによらない）。"""
    template = FormTemplate.objects.get(form_id=form_id, language="ja")
    assert template.method == FormMethod.XLSX_CELLS
    data: dict[str, Any] = {}
    for item in text_targets(template):
        if item.get("format") == "template":
            for spec in item["keys"].values():
                data[spec["key"]] = longest(spec.get("format", "text"))
        else:
            data[item["key"]] = longest(item.get("format", "text"))
    sizes = written_sizes(template, data)
    assert len(sizes) > 10
    assert too_tall(sizes) == []


def test_word_tags_are_not_in_fixed_height_rows() -> None:
    found = []
    for path in sorted(ROOT.glob("*/*/template.docx")):
        xml = zipfile.ZipFile(path).read("word/document.xml").decode("utf-8")
        for row in re.findall(r"<w:tr\b.*?</w:tr>", xml, re.S):
            if 'w:hRule="exact"' in row and "{{" in "".join(
                re.findall(r"<w:t[^>]*>([^<]*)</w:t>", row)
            ):
                found.append(path.parent.name)
    assert found == []


def test_the_address_is_merged_only_for_the_pdf(loaded: None) -> None:
    """Ⅱ２ の住所（13pt を 8.25pt の 1 行に書く）は一時の結合の範囲の左上で、結合した範囲の高さには
    標準の 13pt が収まる。字は小さくしない（A6 の案 (e)）。"""
    template = FormTemplate.objects.get(form_id="1-17", language="ja")
    assert form_generation.merged_targets(template) == MERGED_FOR_PDF
    path = form_templates.template_file(template)
    alone = form_xlsx.cell_fonts(path, sorted(MERGED_FOR_PDF))
    merged = form_xlsx.cell_fonts(
        path, sorted(MERGED_FOR_PDF), form_generation.text_areas(template)
    )
    for target in MERGED_FOR_PDF:
        assert alone[target].size == 13 and alone[target].height_pt < 9
        assert merged[target].height_pt >= 13 * form_xlsx.LINE_SPACING


def test_the_outsourced_address_of_iv_4_is_always_empty(loaded: None) -> None:
    """Ⅳ４ の委託先の住所（AO371）は結合しない。

    委託が有の行は生成不可で、委託が無ければ空を書く。
    """
    template = FormTemplate.objects.get(form_id="1-17", language="ja")
    rules = {rule.get("name"): rule.get("severity") for rule in template.mapping.rules}
    assert rules["plan_not_outsourced"] == "block"
    item = next(i for i in template.mapping.definition["fields"] if i["target"] in ALWAYS_EMPTY)
    assert item["key"] == "plan.4.main.address"
    assert not ALWAYS_EMPTY & form_generation.merged_targets(template)


def test_fit_uses_the_row_height_and_the_area(loaded: None) -> None:
    """字を小さくする処理は、幅と高さの両方を満たす。fit_area は右の空のセルまでの幅を使う。"""
    template = FormTemplate.objects.get(form_id="1-17", language="ja")
    target = "参考様式第１－１７号!O44"
    path = form_templates.template_file(template)
    alone = form_xlsx.cell_fonts(path, [target])[target]
    area = form_xlsx.cell_fonts(path, [target], {target: "O44:BA44"})[target]
    assert alone.width_pt == 13.5 and round(area.width_pt, 1) == 526.5
    assert area.height_pt == alone.height_pt == 8.25
    assert form_xlsx.fitted_size("静岡県ダミー市サンプル町7-8-9", area, 6) == (6.5, False)
    assert form_xlsx.fitted_size("長" * 200, area, 6) == (6, True)
