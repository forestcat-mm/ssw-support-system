"""日中併記の様式（第1-5号・第5-8号・第5-9号。T38、ADR-0027）。

- 元は入管の中国語の版の 1 ファイル。様式の部分を道具で取り出し（extracted.docx）、差込タグを置く。
- 併記の版の日本語の文は、現行の日本語の版と同じ（署名の欄の行の併記だけが違う）。
  第1-6号・第1-17号の併記の版は現行と違うので使わない（日本語の版だけを作り、画面に注意を出す）。
- 生成履歴は版の言語（ja-zh）、交付記録の書面の言語は生成物の版の言語から。
"""

import datetime
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings

from apps.core.models import SupportLanguage
from apps.forms_engine.models import FormTemplate, GeneratedDocument, GenerationStatus
from apps.support.models import DeliveryKind
from services import form_data_briefing, form_docx, form_generation, form_languages, form_templates
from tests.core.factories import make_user, verified_client
from tests.forms_engine import pdfcheck
from tests.forms_engine.test_form_briefing import worker  # noqa: F401  （fixture）
from tests.forms_engine.test_form_contract import body_texts
from tests.support.test_briefings import create, guidance

pytestmark = pytest.mark.django_db

ROOT = Path(settings.FORM_TEMPLATES_ROOT)
MAPPINGS = Path(settings.FORM_MAPPINGS_ROOT)
TOOLS = ROOT.parent.parent / "tools" / "forms"
FOLDER = "2026-10-18.ja-zh"
FORMS = ("1-5", "5-8", "5-9")
JAPANESE = {
    "1-5": "1-5/2026-10-18",
    "5-8": "5-8/2026-10-17",
    "5-9": "5-9/2026-10-17",
}
# 署名の欄の行は、併記の版では日本語と中国語が同じ行に並ぶ（日本語の文の比べから外す）
SIGNATURE_LINES = {"1-5": ["甲㊞乙"], "5-8": ["特定技能外国人の署名年月日"], "5-9": []}


def folder(form_id: str) -> Path:
    return ROOT / form_id / FOLDER


def run(tool: str, *args: str) -> str:
    result = subprocess.run(
        [sys.executable, str(TOOLS / tool), *args], capture_output=True, text=True, check=True
    )
    return result.stdout


def normalized(text: str) -> str:
    return re.sub(r"[\s　]+", "", text).replace("、", "，")


# --- 道具の出力 -------------------------------------------------------------------------------


@pytest.mark.parametrize("form_id", FORMS)
def test_extraction_and_template_are_reproduced_by_the_tools(tmp_path: Path, form_id: str) -> None:
    run(
        "extract_zh_forms.py",
        str(folder(form_id) / "original.docx"),
        form_id,
        str(tmp_path / "extracted.docx"),
    )
    assert (tmp_path / "extracted.docx").read_bytes() == (
        folder(form_id) / "extracted.docx"
    ).read_bytes()
    stdout = run("make_zh_templates.py", form_id, str(tmp_path))
    assert (tmp_path / "template.docx").read_bytes() == (
        folder(form_id) / "template.docx"
    ).read_bytes()
    assert stdout == (MAPPINGS / form_id / "2026-10-18.ja-zh.json").read_text("utf-8")


@pytest.mark.parametrize("form_id", FORMS)
def test_extracted_part_is_the_form_in_the_official_file(form_id: str) -> None:
    """取り出した本文は、元の 1 ファイルの見出しから次の見出しまでの段落と同じ。"""
    whole = body_texts(folder(form_id) / "original.docx")
    part = body_texts(folder(form_id) / "extracted.docx")
    heading = {
        "1-5": "参考様式第１－５号",
        "5-8": "参考様式第５－８号",
        "5-9": "参考様式第５－９号",
    }
    start = next(i for i, text in enumerate(whole) if text.startswith(heading[form_id]))
    assert whole[start : start + len(part)] == part


@pytest.mark.parametrize("form_id", FORMS)
def test_template_rendered_empty_is_the_extracted_form(tmp_path: Path, form_id: str) -> None:
    out = tmp_path / "empty.docx"
    form_docx.render(folder(form_id) / "template.docx", out, {})
    assert body_texts(out) == body_texts(folder(form_id) / "extracted.docx")


@pytest.mark.parametrize("form_id", FORMS)
def test_japanese_text_is_the_current_japanese_form(form_id: str) -> None:
    """併記の版の日本語の文は、現行の日本語の版と同じ（「、」と「，」は同じとみなす）。"""
    bilingual = "".join(normalized(text) for text in body_texts(folder(form_id) / "extracted.docx"))
    japanese = [
        normalized(text)
        for text in body_texts(ROOT / JAPANESE[form_id] / "original.docx")
        if normalized(text)
    ]
    assert [text for text in japanese if text not in bilingual] == SIGNATURE_LINES[form_id]


def test_outdated_bilingual_forms_differ_from_the_current_japanese_forms(tmp_path: Path) -> None:
    """第1-6号・第1-17号の併記の版は旧い版（現行の文が無い）なので使わない（T38-D1）。"""
    source = folder("1-5") / "original.docx"
    run("extract_zh_forms.py", str(source), "1-6", str(tmp_path / "1-6.docx"))
    run("extract_zh_forms.py", str(source), "1-17", str(tmp_path / "1-17.docx"))
    text_1_6 = "".join(body_texts(tmp_path / "1-6.docx"))
    text_1_17 = normalized("".join(body_texts(tmp_path / "1-17.docx")))
    assert "待遇の相違" not in text_1_6
    assert "待遇の相違" in "".join(body_texts(ROOT / "1-6/2026-10-18/original.docx"))
    # 定期的な面談の実施方法の選択肢が違う（現行は「オンライン／対面」）
    assert "対面とオンラインの併用" in text_1_17
    with zipfile.ZipFile(ROOT / "1-17/2026-10-17/original.xlsx") as book:
        assert "対面とオンラインの併用" not in book.read("xl/sharedStrings.xml").decode()
    assert form_languages.OUTDATED_BILINGUAL == {"1-6", "1-17"}
    assert not (ROOT / "1-6" / FOLDER).exists() and not (ROOT / "1-17" / FOLDER).exists()


@pytest.mark.parametrize("form_id", FORMS)
def test_pages_are_the_same_as_the_official_file(form_id: str) -> None:
    """取り出しと空の差し込みの頁数は、元の 1 ファイルでのこの様式の頁数（2 頁）と同じ。"""
    blank = pdfcheck.blank_pdf(folder(form_id) / "extracted.docx")
    empty = pdfcheck.blank_from_template(folder(form_id) / "template.docx")
    assert len(pdfcheck.pdf_text(blank)) == len(pdfcheck.pdf_text(empty)) == 2


def test_bilingual_versions_are_loaded_with_their_language(loaded: None) -> None:
    for form_id in FORMS:
        template = FormTemplate.objects.get(form_id=form_id, language="ja-zh")
        assert template.valid_from == datetime.date(2026, 10, 18)
        assert template.requires_signed_copy is True
        assert template.manifest["source"]["url"].endswith("001339744.docx")
    assert form_templates.version_stem({"valid_from": "2026-10-18", "language": "ja-zh"}) == FOLDER
    assert form_languages.available("5-9") == ["ja", "ja-zh"]
    assert form_languages.available("1-6") == ["ja"]


def test_wrong_folder_of_a_bilingual_version_stops_loading(repo_copy: tuple[Path, Path]) -> None:
    templates, _mappings = repo_copy
    (templates / "5-9" / FOLDER).rename(templates / "5-9" / "2026-10-18-zh")
    with pytest.raises(form_templates.FormTemplateError):
        form_templates.load_all()


# --- 生成と交付記録 ---------------------------------------------------------------------------


def test_chinese_worker_gets_the_bilingual_confirmation(worker: Any) -> None:  # noqa: F811
    briefing = create(worker, guidance(worker))
    assert form_languages.default_for(worker, "5-9") == "ja-zh"
    data = form_data_briefing.build(briefing)
    document = form_generation.request(
        "5-9",
        target=briefing,
        data=data,
        basis_on=briefing.record.occurred_on,
        by=None,
        language="ja-zh",
    )
    document.refresh_from_db()
    assert document.status == GenerationStatus.COMPLETED, document.failure_reason
    assert document.template.language == "ja-zh"
    assert json.loads(document.snapshot)["language"] == "ja-zh"
    assert document.page_count == 2
    text = "\n".join(body_texts(Path(document.filled_file.path)))
    assert "事先引导确认书" in text and "佐藤 花子" in text
    # 中国語の行の日時の欄にも同じ値が入る（修正 1 回目 A7）
    year = str(briefing.record.occurred_on.year).translate(
        str.maketrans("0123456789", "０１２３４５６７８９")
    )
    chinese_rows = [line for line in text.splitlines() if "分起至" in line]
    assert chinese_rows and year in chinese_rows[0]
    # 交付記録の書面の言語は、生成物の版の言語（日本語と中国語）
    client = verified_client(make_user("tanto38", roles=("support_staff",)))
    page = client.get(
        f"/workers/{worker.pk}/support/deliveries/new/{DeliveryKind.OTHER}/?generated={document.pk}"
    )
    codes = set(
        SupportLanguage.objects.filter(
            pk__in=page.context["form"].initial["languages"]
        ).values_list("code", flat=True)
    )
    assert codes == {"ja", "zh-hans"}
    shown = client.get(f"/forms/documents/{document.pk}/").content.decode()
    assert 'data-language="ja-zh"' in shown


def test_generation_screens_offer_the_language_and_the_notice(worker: Any) -> None:  # noqa: F811
    briefing = create(worker, guidance(worker))
    client = verified_client(make_user("tanto38", roles=("support_staff",)))
    url = f"/workers/{worker.pk}/support/briefings/{briefing.pk}/generate/"
    page = client.get(url)
    assert page.context["form"]["language"].initial == "ja-zh"
    client.post(url, {page.context["once_field"]: page.context["once_token"], "language": "ja-zh"})
    document = GeneratedDocument.objects.get(worker=worker)
    assert document.template.language == "ja-zh"
    contract = worker.employment_contracts.get()
    version = contract.versions.get()
    page_1_6 = client.get(
        f"/workers/{worker.pk}/contracts/{contract.pk}/versions/{version.version}/generate/1-6/"
    ).content.decode()
    assert 'id="bilingual-notice"' in page_1_6 and 'name="language"' not in page_1_6
    page_1_5 = client.get(
        f"/workers/{worker.pk}/contracts/{contract.pk}/versions/{version.version}/generate/1-5/"
    )
    assert page_1_5.context["form"]["language"].initial == "ja-zh"
    assert 'id="bilingual-notice"' not in page_1_5.content.decode()
    japanese = SupportLanguage.objects.get(code="ja")
    worker.support_language = japanese
    worker.save()
    page = client.get(url)
    assert page.context["form"]["language"].initial == "ja"


def test_extracted_files_keep_the_other_parts_of_the_official_file() -> None:
    with (
        zipfile.ZipFile(folder("5-9") / "original.docx") as original,
        zipfile.ZipFile(folder("5-9") / "extracted.docx") as part,
    ):
        names = [name for name in original.namelist() if name != "word/document.xml"]
        assert names == [name for name in part.namelist() if name != "word/document.xml"]
        for name in names:
            assert original.read(name) == part.read(name), name


def test_notice_only_for_chinese_workers_on_outdated_forms(worker: Any) -> None:  # noqa: F811
    assert form_languages.outdated_notice(worker, "1-17") is True
    assert form_languages.outdated_notice(worker, "1-6") is True
    assert form_languages.outdated_notice(worker, "1-5") is False
    worker.support_language = SupportLanguage.objects.get(code="ja")
    assert form_languages.outdated_notice(worker, "1-17") is False
    assert form_languages.delivery_language_codes("ja") == ("ja",)
