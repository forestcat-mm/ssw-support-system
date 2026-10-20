"""第1-6号・第1-17号の中国語（簡体字）の訳文の版（自作。T43、ADR-0035）。

- 訳文の版は道具の出力そのもの（手で直さない）。日本語の版と同じ形（タグ・セル・頁数）で、
  固定の文だけが中国語。
- 生成のときは、訳の表の文と同じ値だけを中国語にし、ほかの日本語の値は日本語のまま（警告）。
- 訳文は日本語の版とは別の生成物。申請の書類の一覧の生成の有無には数えず、
  統合 PDF では日本語の直後に綴じる。
"""

import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from pypdf import PdfReader

from apps.forms_engine.models import FormTemplate, GeneratedDocument, GenerationStatus
from apps.support.models import SupportPlanVersion
from apps.workers.models import ForeignWorker
from services import (
    application_sets,
    form_data_contract,
    form_generation,
    form_languages,
    form_samples,
    form_translations,
    submission_bundles,
)
from tests.core.factories import make_user, verified_client
from tests.forms_engine import pdfcheck
from tests.forms_engine.conftest import BASE

pytestmark = pytest.mark.django_db

ROOT = Path(settings.FORM_TEMPLATES_ROOT)
MAPPINGS = Path(settings.FORM_MAPPINGS_ROOT)
REPO = ROOT.parent.parent
FORMS = {"1-6": "2026-10-18", "1-17": "2026-10-17"}
# かな（項目の記号のア・イ・ウは欄の名前としてそのまま使う。注意の文が「Ⅳ欄３アｄ」と引く）
KANA = re.compile(r"[\u3041-\u3096\u30a1\u30a3\u30a5\u30a7-\u30fa]")
TAG = re.compile(r'\{\{\s*(\w+)\s+or\s+"[^"]*"\s*\}\}')


def outputs(root: Path) -> list[Path]:
    files = [root / "docs" / "translations" / "values.zh-hans.md"]
    for form_id, valid_from in FORMS.items():
        folder = root / "templates" / "forms" / form_id / f"{valid_from}.zh-hans"
        files += sorted(folder.iterdir())
        files += [
            root / "mappings" / form_id / f"{valid_from}.zh-hans.json",
            root / "mappings" / form_id / f"{valid_from}.zh-hans.rules.json",
            root / "docs" / "translations" / f"{form_id}.zh-hans.md",
        ]
    return files


def test_translated_versions_are_reproduced_by_the_tool(tmp_path: Path) -> None:
    subprocess.run(
        [sys.executable, str(REPO / "tools" / "forms" / "make_translation_templates.py"), tmp_path],
        check=True,
        cwd=REPO,
    )
    for path in outputs(REPO):
        made = tmp_path / path.relative_to(REPO)
        assert made.read_bytes() == path.read_bytes(), path


def texts_of(docx: Path) -> str:
    with zipfile.ZipFile(docx) as archive:
        xml = archive.read("word/document.xml").decode()
    return "".join(re.findall(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>", xml))


def test_word_translation_keeps_the_tags_and_has_no_japanese() -> None:
    japanese = texts_of(ROOT / "1-6" / "2026-10-18" / "template.docx")
    chinese = texts_of(ROOT / "1-6" / "2026-10-18.zh-hans" / "template.docx")
    assert sorted(TAG.findall(japanese)) == sorted(TAG.findall(chinese))
    with zipfile.ZipFile(ROOT / "1-6" / "2026-10-18.zh-hans" / "template.docx") as archive:
        xml = archive.read("word/document.xml").decode()
        styles = archive.read("word/styles.xml").decode()
    # 字体は中国語の字体にそろえる（T43-D7 の直し）
    fonts = {
        font
        for tag in re.findall(r"<w:rFonts\b[^>]*>", xml + styles)
        for font in re.findall(r'w:(?:ascii|hAnsi|eastAsia|cs)="([^"]+)"', tag)
    }
    assert fonts == {"Noto Serif CJK SC"}
    assert set(re.findall(r'<w:lang\b[^>]*w:eastAsia="([^"]+)"', xml + styles)) <= {"zh-CN"}
    # 下線の付いた差込タグだけの文字の要素は元の書式のまま残る（T43-D9 の直し）
    host_run = re.search(r"<w:r[ >](?:(?!</w:r>).)*\{\{ host_name or[^<]*</w:t></w:r>", xml, re.S)
    assert host_run is not None and "<w:u " in host_run.group(0)
    total = re.search(r"<w:r[ >](?:(?!</w:r>).)*约\{\{ monthly_total[^<]*</w:t></w:r>", xml, re.S)
    assert total is not None and "<w:u " in total.group(0)
    assert not KANA.search(TAG.sub("", chinese))
    assert "雇佣条件书" in chinese and "（译文 / 翻訳文）" in chinese


def test_excel_translation_changes_only_the_shared_strings() -> None:
    japanese = ROOT / "1-17" / "2026-10-17" / "original.xlsx"
    chinese = ROOT / "1-17" / "2026-10-17.zh-hans" / "original.xlsx"
    with zipfile.ZipFile(japanese) as ja, zipfile.ZipFile(chinese) as zh:
        assert ja.namelist() == zh.namelist()
        for name in ja.namelist():
            if name not in ("xl/sharedStrings.xml", "xl/styles.xml"):
                assert ja.read(name) == zh.read(name), name

        # スタイルは字体の名前・文字セット・テーマの指定だけが違う（T43-D7 の直し）
        def fontless(xml: bytes) -> str:
            text = xml.decode()
            text = re.sub(
                r'<name val="[^"]*"/>|<charset val="\d+"/>|<scheme val="[^"]*"/>', "", text
            )
            return text

        assert fontless(ja.read("xl/styles.xml")) == fontless(zh.read("xl/styles.xml"))
        assert set(re.findall(r'<name val="([^"]*)"/>', zh.read("xl/styles.xml").decode())) <= {
            "Noto Serif CJK SC",
            "Noto Sans CJK SC",
        }
        strings = zh.read("xl/sharedStrings.xml").decode()
    assert not KANA.search(re.sub(r"<[^>]+>", "", strings))
    assert "支援计划书（译文 / 翻訳文）" in strings


def test_mapping_is_the_japanese_one_with_translated_printed_texts() -> None:
    for form_id, valid_from in FORMS.items():
        japanese = json.loads((MAPPINGS / form_id / f"{valid_from}.json").read_text("utf-8"))
        chinese = json.loads((MAPPINGS / form_id / f"{valid_from}.zh-hans.json").read_text("utf-8"))
        assert [f["target"] for f in japanese["fields"]] == [f["target"] for f in chinese["fields"]]
        assert [f.get("key") for f in japanese["fields"]] == [
            f.get("key") for f in chinese["fields"]
        ]
        rules = json.loads(
            (MAPPINGS / form_id / f"{valid_from}.zh-hans.rules.json").read_text("utf-8")
        )
        assert rules["rules"][-1]["name"] == "untranslated_values"


def test_values_are_translated_only_when_registered() -> None:
    data = {
        "plan.1.main.yes_note": "入国後速やかに",
        "plan.2.a.yes_note": "毎週の朝礼で説明",  # 自由記述（訳の表に無い）
        "host.name": "サンプル株式会社",  # 訳さない欄
        "c.holiday_weekdays": "土・日",
        "c.categories": "機械金属加工、金属表面処理",
        "c.wage_closing_day": "■　毎月　　末日、　□　毎月　　　日",
        "consultation.mon.start": "9時30分",
        "number": 3,
    }
    result = form_translations.translate(data, "zh-hans")
    assert result["plan.1.main.yes_note"] == "入境后尽快"
    assert result["plan.2.a.yes_note"] == "毎週の朝礼で説明"
    assert result["host.name"] == "サンプル株式会社"
    assert result["c.holiday_weekdays"] == "六、日"
    assert result["c.categories"] == "机械金属加工、金属表面处理"
    assert result["c.wage_closing_day"].startswith("■　每月　　月末")
    assert result["consultation.mon.start"] == "9点30分"
    assert result["number"] == 3
    assert result[form_translations.UNTRANSLATED] == ["plan.2.a.yes_note"]


def test_languages_and_delivery_codes() -> None:
    assert form_languages.delivery_language_codes("zh-hans") == ("zh-hans",)
    assert form_languages.TRANSLATIONS == {"zh-hans"}


# --- 生成 -----------------------------------------------------------------------------


def pdf_fonts(document: GeneratedDocument) -> set[str]:
    """PDF に埋め込んだ字体の名前（Noto CJK の集まりは、どの言語の face でも名前が jp になる）。"""
    found: set[str] = set()
    for page in PdfReader(document.pdf_file.path).pages:
        resources: Any = page["/Resources"]
        fonts: Any = resources.get("/Font") or {}
        found |= {str(font.get_object()["/BaseFont"]) for font in fonts.values()}
    return found


def pages(document: GeneratedDocument) -> int:
    assert document.page_count is not None
    return document.page_count


def test_generation_of_both_translations(seeded: list[ForeignWorker]) -> None:
    worker = seeded[1]  # 2 番（中国語）
    assert form_languages.is_chinese(worker)
    target, data = form_samples.sample("1-17", worker, BASE)
    assert isinstance(target, SupportPlanVersion)
    japanese = form_generation.request(
        "1-17", target=target, data=data, basis_on=BASE, by=None, accept_warnings=True
    )
    chinese = form_generation.request(
        "1-17",
        target=target,
        data=data,
        basis_on=BASE,
        by=None,
        language="zh-hans",
        accept_warnings=True,
    )
    japanese.refresh_from_db()
    chinese.refresh_from_db()
    assert chinese.status == GenerationStatus.COMPLETED, chinese.failure_reason
    assert chinese.template.language == "zh-hans" and not chinese.template.requires_signed_copy
    assert pages(chinese) >= pages(japanese)  # 中国語の字体で頁が増えてよい（T43-D7 の直し）
    assert all("NotoSerifCJK" in f or "NotoSansCJK" in f for f in pdf_fonts(chinese))
    snapshot = json.loads(chinese.snapshot)
    assert snapshot["language"] == "zh-hans"
    assert snapshot["data"]["plan.3a.a.yes_note"] == "入境后尽快"
    text = "".join(pdfcheck.pdf_text(Path(chinese.pdf_file.path)))
    assert "翻訳文" in text
    contract = worker.employment_contracts.order_by("pk").first()
    assert contract is not None
    version = contract.versions.order_by("-version").first()
    assert version is not None
    data_1_6 = form_data_contract.build_1_6(version)
    zh_1_6 = form_generation.request(
        "1-6",
        target=version,
        data=data_1_6,
        basis_on=BASE,
        by=None,
        language="zh-hans",
        accept_warnings=True,
    )
    ja_1_6 = form_generation.request(
        "1-6", target=version, data=data_1_6, basis_on=BASE, by=None, accept_warnings=True
    )
    zh_1_6.refresh_from_db()
    ja_1_6.refresh_from_db()
    assert zh_1_6.status == GenerationStatus.COMPLETED, zh_1_6.failure_reason
    assert pages(zh_1_6) >= pages(ja_1_6)
    # 簡体字だけの字も中国語の字体で描く（IPA など日本語の字体が混ざらない。T43-D7 の直し）
    assert all("NotoSerifCJK" in f for f in pdf_fonts(zh_1_6))
    assert "翻訳文" in "".join(pdfcheck.pdf_text(Path(zh_1_6.pdf_file.path)))

    # 申請の書類の一覧: 訳文は生成の有無に数えない。統合 PDF は日本語の直後に訳文
    overview = application_sets.overview(worker, "ext", BASE)
    facts = overview.facts
    assert facts.documents["1-6"] == ja_1_6
    assert application_sets.translation_of(ja_1_6, facts) == zh_1_6
    admin = make_user("admin43", roles=("admin",))
    outline = submission_bundles.outline(overview, admin)
    entry = next(e for e in outline.entries if e.row["how"].get("generate") == "1-6" and e.parts)
    assert [part.source for part in entry.parts][-1] == submission_bundles.Source.TRANSLATION
    assert submission_bundles.Warn.TRANSLATION_MISSING not in entry.warnings


def test_untranslated_values_are_a_warning(seeded: list[ForeignWorker]) -> None:
    worker = seeded[1]
    target, data = form_samples.sample("1-17", worker, BASE)
    data = {**data, "plan.2.a.yes_note": "毎週の朝礼で説明"}
    result = form_generation.check(
        "1-17", target=target, data=data, basis_on=BASE, language="zh-hans"
    )
    assert any(f.rule == "untranslated" for f in result.warnings)
    japanese = form_generation.check("1-17", target=target, data=data, basis_on=BASE)
    assert not any(f.rule == "untranslated" for f in japanese.findings)


def test_plan_generation_screen_offers_the_translation(seeded: list[ForeignWorker]) -> None:
    worker = seeded[1]
    target, _data = form_samples.sample("1-17", worker, BASE)
    assert isinstance(target, SupportPlanVersion)
    client = verified_client(make_user("tanto43", roles=("support_staff",)))
    url = f"/workers/{worker.pk}/support/plans/{target.plan_id}/versions/{target.version}/generate/"
    page = client.get(url)
    content = page.content.decode()
    assert 'value="zh-hans"' in content and 'id="bilingual-notice"' in content
    assert page.context["form"]["language"].initial == "ja"
    template = FormTemplate.objects.get(form_id="1-17", language="zh-hans")
    assert template.manifest["source"]["self_made"] is True
    assert template.manifest["translation"]["of_language"] == "ja"
