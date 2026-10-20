"""電子届出の一括申込用ツール（xlsm）の列の表・書き込み・入力チェックの写し（T50。ADR-0040）。"""

import datetime
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
from services import efile_checks, form_xlsm
from services.form_xlsx import sheet_paths

ROOT = Path(settings.FORM_TEMPLATES_ROOT)
MAPPINGS = Path(settings.FORM_MAPPINGS_ROOT)
TOOLS = ROOT.parent.parent / "tools" / "forms"
VERSION = "2026-04-01"
FORMS = ("efile-3-1", "efile-3-2", "efile-3-3", "efile-3-4", "efile-3-5", "efile-3-7", "efile-4-3")
TODAY = datetime.date(2026, 10, 21)


def columns(form_id: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (MAPPINGS / form_id / f"{VERSION}.columns.json").read_text("utf-8")
    )
    return data


def mapping(form_id: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((MAPPINGS / form_id / f"{VERSION}.json").read_text("utf-8"))
    return data


def original(form_id: str) -> Path:
    return ROOT / form_id / VERSION / "original.xlsm"


@pytest.mark.parametrize("form_id", FORMS)
def test_column_table_is_reproduced_by_the_tool(form_id: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "extract_efile_columns.py"),
            form_id,
            VERSION,
            str(original(form_id)),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == columns(form_id)


@pytest.mark.parametrize("form_id", FORMS)
def test_mapping_is_reproduced_by_the_tool(form_id: str) -> None:
    """欄の対応は道具の出力。列項目TBL のすべての列がデータのキーに当たる。

    当たらなければ道具が止まる。
    """
    result = subprocess.run(
        [sys.executable, str(TOOLS / "make_efile_mappings.py"), form_id, VERSION],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == mapping(form_id)
    targets = {field["target"] for field in mapping(form_id)["fields"]}
    assert {column["column"] for column in columns(form_id)["columns"]} <= targets


@pytest.mark.django_db
def test_tools_are_loaded_as_a_new_method(loaded: None) -> None:
    for form_id in FORMS:
        template = FormTemplate.objects.get(form_id=form_id)
        assert template.method == FormMethod.XLSM_EFILE
        assert template.revision_unknown
        assert template.mapping.definition["columns_table"]["tool"]["sha256"] == (
            template.original_sha256
        )


def test_tables_read_from_the_workbook() -> None:
    table = columns("efile-3-1")
    assert table["sheet"] == "参考様式第３－１号（別紙）"
    assert (table["first_row"], table["rows"]) == (12, 21)
    assert table["organization_cells"] == {"corporate_number": "C4", "name": "C5", "address": "C6"}
    # 表示名は末尾の空白まで写す（「特定技能雇用契約の変更 」）
    assert ["特定技能雇用契約の変更 ", "01"] in table["codes"]["A022"]
    # INDIRECT の親は相対参照で決まる（J は I、AA は Z）
    assert table["lists"]["J"] == {
        "kind": "linked",
        "parent": "I",
        "substitute": True,
        "suffix": "",
    }
    assert table["lists"]["AA"]["parent"] == "Z"
    assert "19.基本賃金" in table["linked"]["賃金"]
    assert columns("efile-3-5")["organization_cells"]["name"] == "G5"
    assert columns("efile-4-3")["lists"]["L"]["suffix"] == "_措置内容"


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
    table = columns("efile-3-4")
    sheet = table["sheet"]
    out = tmp_path / "efile.xlsm"
    rows = [
        {"B": "特定技能外国人の都合", "C": "行方不明", "O": "WANG FANG", "R": "1998", "T": "05"},
        {"B": "特定技能所属機関の都合", "O": "LI <&>"},
    ]
    form_xlsm.write_rows(
        original("efile-3-4"),
        out,
        rows={sheet: rows},
        first_row=table["first_row"],
        formula_column=None,
        cells={f"{sheet}!C5": "株式会社サンプル", f"{sheet}!C6": "〒100-0001\n東京都千代田区"},
    )
    before, after = _parts(original("efile-3-4")), _parts(out)
    assert list(before) == list(after)
    with zipfile.ZipFile(out) as archive:
        path = sheet_paths(archive)[sheet]
    for name in before:
        if name != path:
            assert before[name] == after[name], name  # マクロ・ActiveX・隠しシート・テーブルは同じ
    old, new = before[path].decode(), after[path].decode()
    # 書いた行（12・13）と届出機関の行（5・6）のほかは、入力規則・結合・列の幅まで同じ
    assert _without_rows(old, {5, 6, 12, 13}) == _without_rows(new, {5, 6, 12, 13})
    book = openpyxl.load_workbook(out, keep_vba=True)
    assert book[sheet]["O13"].value == "LI <&>"
    assert book[sheet]["T12"].value == "05"  # 月・日は 2 桁の文字（.Text が書式によらず同じ）
    assert book[sheet]["C5"].value == "株式会社サンプル"
    assert book["業務コードTBL"].sheet_state == "veryHidden"


def _person(form_id: str) -> dict[str, str]:
    """本人の欄の正しい値（列は版の列項目TBL の項目名から）。"""
    values = {
        "英字氏名": "WANG FANG",
        "性別": "女",
        "国籍・地域": "中国",
        "在留カード番号": "AB12345678CD",
        "特定産業分野": "工業製品製造業分野",
        "業務区分": "機械金属加工（工業製品製造業分野）",
    }
    row: dict[str, str] = {}
    for column in columns(form_id)["columns"]:
        if column["name"] in values:
            row[column["column"]] = values[column["name"]]
        if column["name"] == "生年月日":
            year, month, day = column["column"].split(",")
            row |= {year: "1998", month: "05", day: "01"}
    return row


def _date(row: dict[str, str], letters: str, day: datetime.date) -> None:
    year, month, date = letters.split(",")
    row |= {year: f"{day.year:04d}", month: f"{day.month:02d}", date: f"{day.day:02d}"}


VALID_EVENTS: dict[str, dict[str, str]] = {
    "efile-3-1": {
        "B": "特定技能雇用契約の変更 ",
        "I": "賃金",
        "J": "19.基本賃金",
        "K": "月額２０万円",
    },
    "efile-3-2": {"H": "登録支援機関", "I": "15.支援責任者の氏名及び役職", "J": "山田一郎"},
    "efile-3-3": {"B": "支援委託契約の締結", "I": "支援計画の全部の実施の委託"},
    "efile-3-4": {"B": "特定技能外国人の都合", "C": "行方不明", "L": "連絡不可能"},
    "efile-3-5": {"N": columns("efile-3-5")["lists"]["N"]["values"][0], "O": "その他"},
    "efile-3-7": {
        "B": "支援の実施困難",
        "C": "定期的な面談の実施",
        "K": "面談に応じない",
        "L": "実施再開（再開見込み含む）",
    },
    "efile-4-3": {
        "B": "その他の特異事案",
        "C": "特定技能所属機関における基準不適合等の問題を把握",
        "K": "残業代の未払い",
        "L": "問題解決（解決見込み含む）",
    },
}
DATES = {
    "efile-3-1": "C,E,G",
    "efile-3-2": "B,D,F",
    "efile-3-3": "C,E,G",
    "efile-3-4": "E,G,I",
    "efile-3-5": "B,D,F",
    "efile-3-7": "E,G,I",
    "efile-4-3": "E,G,I",
}


def valid_row(form_id: str) -> dict[str, str]:
    row = _person(form_id) | VALID_EVENTS[form_id]
    _date(row, DATES[form_id], datetime.date(2026, 10, 1))
    return row


@pytest.mark.parametrize("form_id", FORMS)
def test_a_complete_row_passes(form_id: str) -> None:
    findings = efile_checks.check_rows(columns(form_id), [valid_row(form_id)], TODAY)
    assert [f for f in findings if f.blocking] == []


@pytest.mark.parametrize("form_id", FORMS)
def test_every_rule_of_the_column_table_is_checked(form_id: str) -> None:
    """列項目TBL の必須・業務コード・文字種・桁数（桁数一致）の列が、すべて確かめられる。"""
    table = columns(form_id)
    empty = {f.column for f in efile_checks.check_rows(table, [{}], TODAY)}
    assert {c["column"] for c in table["columns"] if c["required"]} <= empty
    coded = [c for c in table["columns"] if c["code"]]
    row = valid_row(form_id) | {c["column"]: "×" for c in coded if "," not in c["column"]}
    wrong = {(f.column, f.message) for f in efile_checks.check_rows(table, [row], TODAY)}
    for column in coded:
        assert (column["column"], efile_checks.NO_CODE) in wrong, column["name"]
    typed = [
        c
        for c in table["columns"]
        if not c["code"] and "," not in c["column"] and c["char_type"] != efile_checks.WIDE
    ]
    row = valid_row(form_id) | {c["column"]: "ａ" * ((c["length"] or 1) + 1) for c in typed}
    wrong_columns = {f.column for f in efile_checks.check_rows(table, [row], TODAY)}
    assert {c["column"] for c in typed} <= wrong_columns
    limited = [
        c
        for c in table["columns"]
        if c["char_type"] == efile_checks.WIDE and c["length"] and c["length"] < 9999
    ]
    row = valid_row(form_id) | {c["column"]: "あ" * (c["length"] + 1) for c in limited}
    too_long = {(f.column, f.message) for f in efile_checks.check_rows(table, [row], TODAY)}
    for column in limited:
        assert (column["column"], f"{column['length']}{efile_checks.DIGITS}") in too_long


def test_dates_follow_the_macro() -> None:
    table = columns("efile-3-1")
    row = valid_row("efile-3-1")
    _date(row, "S,U,W", datetime.date(2026, 10, 22))  # 生年月日が未来
    messages = {(f.column, f.message) for f in efile_checks.check_rows(table, [row], TODAY)}
    assert ("S,U,W", efile_checks.DATE_FUTURE) in messages
    row |= {"S": "2026", "U": "02", "W": "30"}
    messages = {(f.column, f.message) for f in efile_checks.check_rows(table, [row], TODAY)}
    assert ("S,U,W", efile_checks.DATE) in messages
    row |= {"S": "1899", "U": "12", "W": "31"}
    messages = {(f.column, f.message) for f in efile_checks.check_rows(table, [row], TODAY)}
    assert ("S,U,W", efile_checks.DATE_RANGE) in messages
    row |= {"S": "1998", "U": "5", "W": "1"}  # 桁数一致（10 桁）の検査に当たる
    messages = {(f.column, f.message) for f in efile_checks.check_rows(table, [row], TODAY)}
    assert ("S,U,W", f"10{efile_checks.DIGITS_EXACT}") in messages
    assert efile_checks.joined({"C": "2026", "G": "01"}, "C,E,G", "/") == "2026//01"


def test_individual_checks_of_each_tool() -> None:
    def messages(form_id: str, row: dict[str, str]) -> set[str]:
        found = efile_checks.check_rows(columns(form_id), [row], TODAY)
        return {f.column for f in found if f.blocking}

    row = valid_row("efile-3-1") | {"B": "特定技能雇用契約の終了"}
    assert {"L", "M", "N"} <= messages("efile-3-1", row)  # 終了は事由・詳細・その他の理由
    row |= {"L": "特定技能外国人の都合", "M": "10.自己都合退職", "N": "一身上の都合"}
    assert not {"L", "M", "N"} & messages("efile-3-1", row)
    row |= {"M": "02.経営上の都合"}  # 親（終了の事由）に合わない詳細
    assert "M" in messages("efile-3-1", row)
    row = valid_row("efile-3-1") | {"B": "新たな特定技能雇用契約の締結"}
    assert "O" in messages("efile-3-1", row)

    row = valid_row("efile-3-3") | {
        "B": "支援委託契約の終了",
        "L": "特定技能所属機関の都合による終了",
    }
    assert "M" in messages("efile-3-3", row)
    row |= {"M": "その他"}
    assert "N" in messages("efile-3-3", row)  # 小分類「その他」(07) ならその他の理由
    row = valid_row("efile-3-3") | {"AA": "19"}
    assert "AA" in messages("efile-3-3", row)  # 登録番号は前 2 桁と後 6 桁の両方

    row = valid_row("efile-3-4") | {"C": "経営上の都合"}  # 外国人の都合の表に無い区分
    assert "C" in messages("efile-3-4", row)
    row = valid_row("efile-3-4")
    del row["C"]
    assert "C" in messages("efile-3-4", row)  # 事由の区分は必須

    row = valid_row("efile-4-3") | {"C": "その他", "L": "問題解決困難"}
    assert {"D", "M"} <= messages("efile-4-3", row)
    row = valid_row("efile-3-7")
    for letters in ("K", "E", "G", "I"):
        del row[letters]
    assert {"K", "E,G,I"} <= messages("efile-3-7", row)


def test_longer_than_the_heading_is_a_warning() -> None:
    table = columns("efile-3-1")
    advice = {
        field["target"]: field["advice_length"]
        for field in mapping("efile-3-1")["fields"]
        if field.get("advice_length")
    }
    row = valid_row("efile-3-1") | {"K": "あ" * 21}
    found = efile_checks.check_rows(table, [row], TODAY, advice)
    assert [(f.column, f.blocking) for f in found] == [("K", False)]
