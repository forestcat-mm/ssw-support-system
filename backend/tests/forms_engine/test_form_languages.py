"""日中併記の様式（第1-5号・第1-6号・第5-8号・第5-9号。T38、ADR-0027）。

- 元は入管の中国語の版の 1 ファイル。様式の部分を道具で取り出し（extracted.docx）、差込タグを置く。
- 併記の版の日本語の文は、現行の日本語の版と同じ（署名の欄の行の併記だけが違う）。
  第1-6号の併記の版は旧い版なので、足りない事項を補足の頁（自作の日中併記）で付け足して使う（A）。
  第1-17号の併記の版は差が多いので使わず、自作の訳文を例外として使う（B。画面に注意を出す）。
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
    """第1-6号・第1-17号の併記の版は旧い版（現行の文が無い）。第1-6号は補足の頁で補い（A）、
    第1-17号は使わない（B。2026-10-20、ADR-0027 の追記）。"""
    source = folder("1-5") / "original.docx"
    run("extract_zh_forms.py", str(source), "1-17", str(tmp_path / "1-17.docx"))
    text_1_6 = "".join(body_texts(folder("1-6") / "extracted.docx"))
    text_1_17 = normalized("".join(body_texts(tmp_path / "1-17.docx")))
    assert "待遇の相違" not in text_1_6
    assert "待遇の相違" in "".join(body_texts(ROOT / "1-6/2026-10-18/original.docx"))
    assert "待遇の相違" in "".join(body_texts(folder("1-6") / "supplement.docx"))
    # 定期的な面談の実施方法の選択肢が違う（現行は「オンライン／対面」）
    assert "対面とオンラインの併用" in text_1_17
    with zipfile.ZipFile(ROOT / "1-17/2026-10-17/original.xlsx") as book:
        assert "対面とオンラインの併用" not in book.read("xl/sharedStrings.xml").decode()
    assert form_languages.OUTDATED_BILINGUAL == {"1-17"}
    assert (ROOT / "1-6" / FOLDER).exists() and not (ROOT / "1-17" / FOLDER).exists()


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
    # 第1-6号: 日中併記（旧い版＋補足の頁）と、自作の訳文（T43。残す）
    assert form_languages.available("1-6") == ["ja", "ja-zh", "zh-hans"]


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
    )
    # 第1-6号は日中併記（旧い版＋補足の頁）が初期値。自作の訳文も選べる（2026-10-20）
    assert page_1_6.context["form"]["language"].initial == "ja-zh"
    content_1_6 = page_1_6.content.decode()
    assert 'id="bilingual-notice"' not in content_1_6 and 'value="zh-hans"' in content_1_6
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
    assert form_languages.outdated_notice(worker, "1-6") is False  # 併記の版＋補足の頁
    assert form_languages.outdated_notice(worker, "1-5") is False
    worker.support_language = SupportLanguage.objects.get(code="ja")
    assert form_languages.outdated_notice(worker, "1-17") is False
    assert form_languages.delivery_language_codes("ja") == ("ja",)


def test_bilingual_1_5_is_printed_on_one_page(loaded: None) -> None:
    """第1-5号（日中併記）は、PDF にする一時の写しでだけ詰めて 1 枚に収める（ADR-0032 の追記）。"""
    from tests.forms_engine.test_form_contract import BASIS, data_for, explain, full_version

    version = full_version()
    explain(version)
    document = form_generation.request(
        "1-5",
        target=version,
        data=data_for("1-5", version),
        basis_on=BASIS,
        by=None,
        language="ja-zh",
        accept_warnings=True,
    )
    document.refresh_from_db()
    assert document.status == GenerationStatus.COMPLETED, document.failure_reason
    assert document.page_count == 1
    # 保存する文書（差し込んだもの）は公式の組み方のまま
    with zipfile.ZipFile(document.filled_file.path) as archive:
        xml = archive.read("word/document.xml").decode()
    assert 'w:linePitch="360"' in xml and 'w:top="851"' in xml
    assert '<w:sz w:val="22"/>' in xml and '<w:sz w:val="20"/>' not in xml
    # 日本語だけの第1-5号は、もとから 1 頁（詰めない）
    japanese = FormTemplate.objects.get(form_id="1-5", language="ja")
    assert "pdf_layout" not in japanese.manifest


def test_pdf_layout_keeps_the_body_at_9pt_or_more() -> None:
    assert form_docx.pdf_layout_errors({"line_pitch": 300, "font_sizes": {"22": 18}}) == []
    assert form_docx.pdf_layout_errors({"font_sizes": {"22": 17}})
    assert form_docx.pdf_layout_errors({"line_pitch": 0})
    assert form_docx.pdf_layout_errors({"columns": 2})


def test_pdf_layout_changes_only_the_copy(tmp_path: Path) -> None:
    source = folder("1-5") / "template.docx"
    copy = tmp_path / "copy.docx"
    layout = json.loads((folder("1-5") / "manifest.json").read_text("utf-8"))["pdf_layout"]
    form_docx.with_pdf_layout(source, copy, layout)
    with zipfile.ZipFile(copy) as archive:
        xml = archive.read("word/document.xml").decode()
    assert 'w:linePitch="300"' in xml and 'w:top="567"' in xml and 'w:bottom="567"' in xml
    assert '<w:sz w:val="22"/>' not in xml and '<w:sz w:val="32"/>' in xml  # 見出しはそのまま
    assert body_texts(copy) == body_texts(source)  # 文は変えない


# --- 第1-6号（日中併記の旧い版＋補足の頁。2026-10-20、ADR-0027 の追記）--------------------------


def test_bilingual_1_6_is_reproduced_by_the_tools(tmp_path: Path) -> None:
    run(
        "extract_zh_forms.py",
        str(folder("1-6") / "original.docx"),
        "1-6",
        str(tmp_path / "extracted.docx"),
    )
    assert (tmp_path / "extracted.docx").read_bytes() == (
        folder("1-6") / "extracted.docx"
    ).read_bytes()
    stdout = run("make_zh_templates.py", "1-6", str(tmp_path))
    assert (tmp_path / "template.docx").read_bytes() == (
        folder("1-6") / "template.docx"
    ).read_bytes()
    assert stdout == (MAPPINGS / "1-6" / "2026-10-18.ja-zh.json").read_text("utf-8")
    run("make_zh_supplement.py", "1-6", str(tmp_path))
    assert (tmp_path / "supplement.docx").read_bytes() == (
        folder("1-6") / "supplement.docx"
    ).read_bytes()


def test_bilingual_1_6_has_the_same_data_keys_as_the_japanese_form() -> None:
    japanese = json.loads((MAPPINGS / "1-6" / "2026-10-18.json").read_text("utf-8"))
    bilingual = json.loads((MAPPINGS / "1-6" / "2026-10-18.ja-zh.json").read_text("utf-8"))
    assert {f["key"] for f in bilingual["fields"]} == {f["key"] for f in japanese["fields"]}
    # 中国語の行の欄は名前に _zh を付け、中国語の行の欄の幅で詰める（チェックは同じタグ）
    zh = [f for f in bilingual["fields"] if f["target"].endswith("_zh")]
    assert zh and all(f["format"] != "check" for f in zh)
    with zipfile.ZipFile(folder("1-6") / "template.docx") as archive:
        template = archive.read("word/document.xml").decode()
    with zipfile.ZipFile(folder("1-6") / "supplement.docx") as archive:
        supplement = archive.read("word/document.xml").decode()
    for item in bilingual["fields"]:
        tag = "{{ " + item["target"] + " "
        assert tag in template or tag in supplement, item["target"]
    for name in ("treatment_department", "treatment_person", "treatment_phone"):
        assert "{{ " + name + " " in supplement and "{{ " + name + " " not in template


def test_bilingual_1_6_rendered_empty_is_the_extracted_form(tmp_path: Path) -> None:
    out = tmp_path / "empty.docx"
    form_docx.render(folder("1-6") / "template.docx", out, {"annex2": True})
    assert body_texts(out) == body_texts(folder("1-6") / "extracted.docx")


def test_bilingual_1_6_is_generated_with_the_supplement(loaded: None) -> None:
    from tests.forms_engine.test_form_contract import BASIS, data_for, explain, full_version

    version = full_version()
    explain(version)
    data = data_for("1-6", version)
    document = form_generation.request(
        "1-6",
        target=version,
        data=data,
        basis_on=BASIS,
        by=None,
        language="ja-zh",
        accept_warnings=True,
    )
    document.refresh_from_db()
    assert document.status == GenerationStatus.COMPLETED, document.failure_reason
    assert document.template.language == "ja-zh" and document.template.requires_signed_copy
    # 本体と別紙１ で 7 頁、補足の頁が 1 頁（別紙２ は出さない）
    assert document.page_count == 8
    text = "\n".join(body_texts(Path(document.filled_file.path)))
    assert "雇用条件書の補足" in text and "雇佣条件书的补充" in text
    assert data["c.treatment_department"] in text  # Ⅸ４ の窓口は補足の頁に入る
    assert data["c.worker_name"] in text.split("先生/女士")[0]  # 中国語の行にも氏名
    assert "参考様式第１－６号　別紙２" not in text
    # 印刷済みの選択肢の文を置き換える欄は、中国語の行では値の訳の表で中国語にして入れる
    assert "工资结算日" in text and "月末" in text.split("工资结算日")[1].split("\n")[0]
    assert "次月" in text.split("工资支付日")[1].split("\n")[0]


def test_bilingual_1_6_translates_the_choice_fields_of_the_chinese_rows(loaded: None) -> None:
    from tests.forms_engine.test_form_contract import BASIS, data_for, explain, full_version

    version = full_version()
    explain(version)
    data = data_for("1-6", version) | {
        "c.renewal_limit": "有（更新３回まで／通算契約期間５年まで）",
        "c.irregular_holidays": "会社カレンダーによる",
    }
    template = FormTemplate.objects.get(form_id="1-6", language="ja-zh")
    values = form_generation.field_values(
        template.mapping.definition, form_generation.prepared(template, data)
    )
    assert values["renewal_limit"] == "有（更新３回まで／通算契約期間５年まで）"
    assert values["renewal_limit_zh"] == "有（最多更新３次／总合同期限５年为止）"
    # 曜日は印刷済みの「周」も欄に含めて「每周六、日」にする（「每周周土・日」にしない）
    assert values["holiday_weekdays"] == "土・日" and values["holiday_weekdays_zh"] == "六、日"
    assert "每月" in str(values["wage_closing_day_zh"]) and "月末" in str(
        values["wage_closing_day_zh"]
    )
    # 訳の表に無い値は日本語のまま入れ（中国語の行を空にしない）、生成前の検査で警告する
    assert str(values["irregular_holidays_zh"]).startswith("会社カレンダーによる")
    result = form_generation.check(
        "1-6", target=version, data=data, basis_on=BASIS, language="ja-zh"
    )
    assert "untranslated_values" in {finding.code for finding in result.findings}
    japanese = form_generation.check("1-6", target=version, data=data, basis_on=BASIS)
    assert "untranslated_values" not in {finding.code for finding in japanese.findings}
