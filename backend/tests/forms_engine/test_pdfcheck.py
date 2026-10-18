"""段階3の完了条件 A「様式との一致」①の比べ方（文字ごとの数）が、固定の文の欠け・置き換えで落ちる
ことを確かめる（T31・T32 の直し。ADR-0020・0021）。

空の様式の PDF と、固定の文を 1 文欠いた写し・1 文字置き換えた写しを、同じ方法で変換して比べる。
"""

from pathlib import Path

import docx
import openpyxl  # type: ignore[import-untyped]
import pytest
from django.conf import settings

from services import form_convert, form_xlsx
from tests.forms_engine import pdfcheck

ROOT = Path(settings.FORM_TEMPLATES_ROOT)
SHEET = "参考様式第１－１７号"
NOTE_1_17 = "９　Ⅳ欄６イの「対応時間」欄は、対応可能曜日についてその時間帯を記載すること。"


def edited_1_17(tmp_path: Path, name: str, old: str, new: str) -> Path:
    original = ROOT / "1-17" / "2026-10-17" / "original.xlsx"
    text = openpyxl.load_workbook(original)[SHEET]["A715"].value
    assert old in text
    folder = tmp_path / name
    folder.mkdir()
    out = folder / "original.xlsx"
    form_xlsx.write_cells(original, out, {f"{SHEET}!A715": text.replace(old, new, 1)})
    return form_convert.convert(out)


def edited_5_8(tmp_path: Path, name: str, old: str, new: str) -> Path:
    original = ROOT / "5-8" / "2026-10-17" / "original.docx"
    document = docx.Document(str(original))
    paragraph = next(p for p in document.paragraphs if old in p.text)
    for run in paragraph.runs:
        if old in run.text:
            run.text = run.text.replace(old, new, 1)
            break
    else:  # 文が run に分かれているときは、段落の最初の run にまとめる
        text = paragraph.text.replace(old, new, 1)
        for run in paragraph.runs:
            run.text = ""
        paragraph.runs[0].text = text
    folder = tmp_path / name
    folder.mkdir()
    out = folder / "original.docx"
    document.save(str(out))
    return form_convert.convert(out)


@pytest.mark.parametrize(
    ("name", "old", "new", "matches"),
    [
        ("same", NOTE_1_17, NOTE_1_17, True),  # 対照: 同じ文に書き直しても一致する
        ("missing", NOTE_1_17, "", False),  # 固定の文が 1 文欠けた
        ("replaced", "対応可能曜日", "対応可能時間", False),  # 1 文字が別の文字に置き換わった
    ],
)
def test_1_17_fixed_text_check_detects_a_missing_or_replaced_sentence(
    tmp_path: Path, name: str, old: str, new: str, matches: bool
) -> None:
    blank = pdfcheck.blank_pdf(ROOT / "1-17" / "2026-10-17" / "original.xlsx")
    changed = edited_1_17(tmp_path, name, old, new)
    assert pdfcheck.fixed_characters_match(changed, blank, [], []) is matches


@pytest.mark.parametrize(
    ("name", "old", "new", "matches"),
    [
        ("same", "内容を十分に理解しました。", "内容を十分に理解しました。", True),
        ("missing", "内容を十分に理解しました。", "", False),
        ("replaced", "十分に理解", "十分に了解", False),
    ],
)
def test_5_8_fixed_text_check_detects_a_missing_or_replaced_sentence(
    tmp_path: Path, name: str, old: str, new: str, matches: bool
) -> None:
    blank = pdfcheck.blank_pdf(ROOT / "5-8" / "2026-10-17" / "original.docx")
    changed = edited_5_8(tmp_path, name, old, new)
    assert pdfcheck.fixed_characters_match(changed, blank, [], []) is matches
