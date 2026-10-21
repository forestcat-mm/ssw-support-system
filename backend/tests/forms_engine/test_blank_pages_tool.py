"""tools/forms/blank_pages.py（空の様式の頁数・字体・代わりの字体の調べ）の、変換を使わない部分。"""

import sys
from pathlib import Path

from django.conf import settings

TOOLS = Path(settings.FORM_TEMPLATES_ROOT).parent.parent / "tools" / "forms"
sys.path.insert(0, str(TOOLS))
import blank_pages  # type: ignore[import-not-found]  # noqa: E402

ROOT = Path(settings.FORM_TEMPLATES_ROOT)
D = blank_pages.DocChar
# fontconfig の代わり: 規則の無い名前の行き先と、IPA 明朝に無い字（クラウドの VM で確かめたもの）
MATCHED = {"Century": "Inter", "SimHei": "Noto Sans CJK SC"}
FONTS = blank_pages.Fonts(
    blank_pages.read_rules(),
    {blank_pages.key("IPAMincho"), blank_pages.key("Inter")},
    match=lambda name: MATCHED.get(name, ""),
    has_glyph=lambda family, char: not (family == "IPAMincho" and char in "⑴㊞"),
)


def found(doc: list[object], pdf: list[tuple[str, str]]) -> dict[tuple[str, ...], dict[str, int]]:
    return {
        (fb.requested, fb.chosen, fb.reason, fb.how): dict(chars)
        for fb, chars in blank_pages.fallbacks(doc, pdf, FONTS).items()
    }


def test_names_are_compared_without_width_case_and_spaces() -> None:
    assert blank_pages.key("ＭＳ 明朝") == blank_pages.key("MS明朝")
    assert blank_pages.key("Noto Sans CJK JP") == blank_pages.key("NotoSansCJKjp")
    assert blank_pages.embedded_family("/ABCDEF+NotoSansCJKjp-Regular") == "NotoSansCJKjp"
    assert blank_pages.embedded_family("/IPAMincho") == "IPAMincho"


def test_target_follows_the_rules_alternate_names_and_theme_mark() -> None:
    assert FONTS.target("ＭＳ 明朝") == ("IPAMincho", "規則")
    assert FONTS.target("ＭＳ 明朝［テーマ］") == ("IPAMincho", "規則")
    assert FONTS.target("Foo;MS Mincho") == ("IPAMincho", "規則")
    assert FONTS.target("Inter") == ("Inter", "そのまま")
    assert FONTS.target("Century") == (None, "規則なし")


def test_same_chars_in_japanese_and_chinese_runs_hit_the_right_run() -> None:
    """第5-8号の日中併記: 「法律」が日本語（ＭＳ ゴシック）と中国語（SimHei）の run の両方に出る。

    字ごとに当てると、中国語の run の NotoSansCJKsc を日本語の run（数が多い方）のせいにしていた。
    位置で当てれば、日本語の run は規則どおり（IPAGothic）で、SimHei の run だけが出る。
    """
    doc = [D("法", "ＭＳ ゴシック", "游明朝"), D("律", "ＭＳ ゴシック", "游明朝")] * 2
    doc += [D("法", "SimHei", "游明朝"), D("律", "SimHei", "游明朝")]
    pdf = [("法", "IPAGothic"), ("律", "IPAGothic")] * 2
    pdf += [("法", "NotoSansCJKsc"), ("律", "NotoSansCJKsc")]
    assert found(doc, pdf) == {
        ("SimHei", "NotoSansCJKsc", "規則が無い", "位置"): {"法": 1, "律": 1}
    }


def test_reasons_tell_missing_rule_missing_glyph_and_latin_font() -> None:
    doc = [
        D("1", "Century", "Century"),  # 規則の無い名前
        D("⑴", "ＭＳ 明朝", "Century"),  # IPA 明朝に字が無い
        D("□", "ＭＳ 明朝", "Century"),  # w:hint="eastAsia" の記号を欧文の字体で描いた
        D("日", "ＭＳ 明朝", "Century"),  # 規則どおり
        D("月", "ＭＳ 明朝", "Century"),  # 字があるのに別の字体
    ]
    pdf = [
        ("1", "Inter"),
        ("⑴", "WenQuanYiZenHei"),
        ("□", "Inter"),
        ("日", "IPAMincho"),
        ("月", "NotoSansCJKsc"),
        ("※", "Inter"),  # 文書の文字に無い
    ]
    assert found(doc, pdf) == {
        ("Century", "Inter", "規則が無い", "位置"): {"1": 1},
        ("ＭＳ 明朝", "WenQuanYiZenHei", "置き換え先に字が無い", "位置"): {"⑴": 1},
        ("ＭＳ 明朝", "Inter", "欧文の字体で描かれた", "位置"): {"□": 1},
        ("ＭＳ 明朝", "NotoSansCJKsc", "置き換え先で描かれない", "位置"): {"月": 1},
        ("（文書の文字に無い）", "Inter", "文書の文字に無い", "字"): {"※": 1},
    }


def test_chars_not_found_by_position_are_guessed_by_char() -> None:
    """頁ごとのヘッダーは、文書に 1 回、PDF に頁の数だけ出る。2 回目からは字で推定する。"""
    doc = [D("頁", "ＭＳ 明朝", "Century"), D("1", "Century", "Century")]
    pdf = [("頁", "IPAMincho"), ("1", "Inter")] * 2
    assert found(doc, pdf) == {
        ("Century", "Inter", "規則が無い", "位置"): {"1": 1},
        ("Century", "Inter", "規則が無い", "字"): {"1": 1},
    }


def text_of(chars: list[object]) -> str:
    return "".join(item.char for item in chars)  # type: ignore[attr-defined]


def test_docx_chars_take_the_font_from_runs_styles_and_theme() -> None:
    chars = blank_pages.doc_chars(ROOT / "13-1" / "2026-10-18" / "original.docx")
    text = text_of(chars)
    # 「319」は hint だけの run（既定の minorHAnsi = Century）、
    # 「5621」は asciiTheme="minorEastAsia"
    assert {item.requested for item in chars[text.index("319") :][:3]} == {"Century［テーマ］"}
    assert {item.requested for item in chars[text.index("5621") :][:4]} == {"ＭＳ 明朝［テーマ］"}


def test_docx_symbols_with_east_asian_hint_keep_the_latin_font() -> None:
    chars = blank_pages.doc_chars(ROOT / "1-16" / "2026-10-21" / "original.docx")
    box = next(item for item in chars if item.char == "□")
    assert (box.requested, box.latin) == ("ＭＳ 明朝［テーマ］", "Century［テーマ］")


def test_docx_runs_of_the_bilingual_form_keep_their_own_fonts() -> None:
    chars = blank_pages.doc_chars(ROOT / "5-8" / "2026-10-18.ja-zh" / "extracted.docx")
    text = text_of(chars)
    japanese = text.index("私の日本での生活")
    chinese = text.index("有关我在日本生活")
    assert {item.requested for item in chars[japanese:][:8]} == {"ＭＳ ゴシック"}
    assert {item.requested for item in chars[chinese:][:8]} == {"SimHei"}


def test_xlsx_chars_take_the_font_of_the_cell() -> None:
    chars = blank_pages.doc_chars(ROOT / "1-17" / "2026-10-17.zh-hans" / "original.xlsx")
    assert chars and all(
        item.requested in {"Noto Sans CJK SC", "Noto Serif CJK SC"}
        for item in chars
        if blank_pages.is_east_asian(item.char)
    )
