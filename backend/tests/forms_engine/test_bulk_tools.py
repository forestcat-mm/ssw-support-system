"""一括登録用ツール（xlsm）への書き込みの基盤と、入力チェックの写し（T47。ADR-0039）。"""

import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import openpyxl  # type: ignore[import-untyped]
import pytest
from django.conf import settings

from apps.forms_engine.models import FormMethod, FormTemplate
from services import bulk_checks, form_xlsm
from services.form_xlsx import sheet_paths

ROOT = Path(settings.FORM_TEMPLATES_ROOT)
MAPPINGS = Path(settings.FORM_MAPPINGS_ROOT)
TOOLS = ROOT.parent.parent / "tools" / "forms"
VERSION = "2026-06-04"
PROCEDURES = {"bulk-coe": "coe", "bulk-ext": "extension", "bulk-chg": "change"}


def columns(form_id: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (MAPPINGS / form_id / f"{VERSION}.columns.json").read_text("utf-8")
    )
    return data


def original(form_id: str) -> Path:
    return ROOT / form_id / VERSION / "original.xlsm"


@pytest.mark.parametrize("form_id", list(PROCEDURES))
def test_column_table_is_reproduced_by_the_tool(form_id: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "extract_bulk_columns.py"),
            form_id,
            VERSION,
            str(original(form_id)),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == columns(form_id)


@pytest.mark.django_db
def test_tools_are_loaded_as_a_new_method(loaded: None) -> None:
    template = FormTemplate.objects.get(form_id="bulk-coe")
    assert template.method == FormMethod.XLSM_ROWS
    assert template.mapping.definition["columns_table"]["common_sheet"].startswith("申請情報入力(")


def _parts(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {item.filename: archive.read(item.filename) for item in archive.infolist()}


def _without_rows(xml: str, numbers: set[int]) -> str:
    return re.sub(
        r'<row r="(\d+)".*?</row>',
        lambda m: "" if int(m.group(1)) in numbers else m.group(0),
        xml,
        flags=re.S,
    )


def test_written_workbook_keeps_the_macros_and_the_structure(tmp_path: Path) -> None:
    table = columns("bulk-coe")
    common, section_v = table["common_sheet"], "申請情報入力(区分V)"
    people = [
        {"B": "中国 People's Republic of China", "D": "WANG FANG", "C": "19980501"},
        {"B": "ベトナム Viet Nam", "D": "NGUYEN VAN AN", "F": "ハノイ & <テスト>"},
    ]
    out = tmp_path / "bulk.xlsm"
    form_xlsm.write_rows(
        original("bulk-coe"),
        out,
        rows={common: people, section_v: [{"B": "特定技能評価試験 Specified"}, {}]},
        first_row=8,
        formula_column="A",
        cells={"メニュー!D15": table["menu"]["value"]},
    )
    before, after = _parts(original("bulk-coe")), _parts(out)
    assert list(before) == list(after)  # 中のファイルの名前と順
    with zipfile.ZipFile(out) as archive:
        paths = sheet_paths(archive)
    edited = {paths[common], paths[section_v], paths["メニュー"]}
    for name in before:
        if name not in edited:
            assert before[name] == after[name], name  # マクロ・ブック・名前定義・ほかのシートは同じ
    for name in (paths[common], paths[section_v]):
        old, new = before[name].decode(), after[name].decode()
        # 書いた行（8・9）のほかは、保護・入力規則・列の幅・ほかの行まで同じ
        assert _without_rows(old, {8, 9}) == _without_rows(new, {8, 9})
    new_common = after[paths[common]].decode()
    row8 = re.search(r'<row r="8".*?</row>', new_common, re.S)
    assert row8 is not None
    assert "<f>IF(COUNTBLANK(B8:CG8)&lt;&gt;84,ROW()-7,&quot;&quot;)</f><v>1</v>" in row8.group(
        0
    ) or ("ROW()-7" in row8.group(0) and "<v>1</v>" in row8.group(0))
    # 読み戻し（openpyxl は確かめにだけ使う）
    book = openpyxl.load_workbook(out, keep_vba=True)
    assert book[common]["D9"].value == "NGUYEN VAN AN"
    assert book[common]["F9"].value == "ハノイ & <テスト>"
    assert book["メニュー"]["D15"].value == "特定技能 Specified Skilled Worker"
    assert book[common].sheet_state == "veryHidden"
    assert book[common].protection.sheet


def _row(**values: dict[str, str]) -> dict[str, dict[str, str]]:
    return values


def test_checks_follow_the_macro() -> None:
    table = columns("bulk-coe")
    common = table["common_sheet"]
    findings = bulk_checks.check_rows(table, [{}], "coe")
    names = {(f.sheet, f.column) for f in findings}
    assert (common, "B") in names  # 国籍・地域は必須
    assert ("申請情報入力(区分V)", "D") not in names  # 受験地は試験名の条件つき
    person = {
        common: {
            "B": "どこか",  # 表にない表示名
            "C": "1998051",  # 8 桁でない（生年月日は不詳の 99999999 があるので日付の検査はしない）
            "D": "WANG FANG",
            "J": "1234",  # 郵便番号は 7 桁
            "K": "東京都",
            "L": "札幌市中央区",  # 東京都の市区町村ではない
        }
    }
    found = {(f.column, f.message) for f in bulk_checks.check_rows(table, [person], "coe")}
    columns_found = {column for column, _message in found}
    assert {"B", "C", "J", "L"} <= columns_found
    person[common] |= {"B": "中国 People's Republic of China", "C": "19980501", "J": "1000001"}
    person[common]["L"] = "千代田区"
    fixed = {f.column for f in bulk_checks.check_rows(table, [person], "coe") if f.sheet == common}
    assert not {"B", "C", "J", "L"} & fixed


def test_every_rule_of_the_column_table_is_checked() -> None:
    """列項目TBL の、条件の無い必須・業務コード・文字種・桁数の列が、すべて確かめられる。"""
    for form_id, procedure in PROCEDURES.items():
        table = columns(form_id)
        visible = [
            c
            for c in table["columns"]
            if not c["check_parent_column"]
            and (c["sheet"] != "申請情報入力(同時申請)")
            and not (c["rel_column"] and not re.fullmatch(r"AA\d+", c["rel_column"]))
        ]
        required = {(c["sheet"], c["column"]) for c in visible if c["required"] and c["csv_column"]}
        empty = {(f.sheet, f.column) for f in bulk_checks.check_rows(table, [{}], procedure)}
        assert required <= empty, form_id
        coded = [c for c in visible if c["code"]]
        person: dict[str, dict[str, str]] = {}
        for column in coded:
            person.setdefault(column["sheet"], {})[column["column"]] = "×"
        wrong = {(f.sheet, f.column) for f in bulk_checks.check_rows(table, [person], procedure)}
        assert {(c["sheet"], c["column"]) for c in coded} <= wrong, form_id
        typed = [c for c in visible if c["char_types"] and c["char_types"] != ["すべて"]]
        long_text = {c["column"]: "\U0001f600" * ((c["length"] or 1) + 1) for c in typed}
        person = {}
        for column in typed:
            person.setdefault(column["sheet"], {})[column["column"]] = long_text[column["column"]]
        wrong = {(f.sheet, f.column) for f in bulk_checks.check_rows(table, [person], procedure)}
        assert {(c["sheet"], c["column"]) for c in typed} <= wrong, form_id


def test_character_types_and_dates() -> None:
    assert bulk_checks.char_type_ok("ﾊﾝｶｸ", ["半角カナ"])
    assert not bulk_checks.char_type_ok("全角", ["半角数字"])
    assert bulk_checks.char_type_ok("東京　タロウ１", ["漢字", "全角カナ", "全角数字"])
    assert bulk_checks.char_type_ok("20260229", ["日付"]) is False
    assert bulk_checks.char_type_ok("20280229", ["日付"])


@pytest.mark.parametrize("form_id", list(PROCEDURES))
def test_mapping_is_reproduced_by_the_tool(form_id: str) -> None:
    """欄の対応（T48）は道具の出力。必須の欄はすべてデータのキーに当たる（当たらなければ道具が止まる）。"""
    result = subprocess.run(
        [sys.executable, str(TOOLS / "make_bulk_mappings.py"), form_id, VERSION],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == json.loads(
        (MAPPINGS / form_id / f"{VERSION}.json").read_text("utf-8")
    )
