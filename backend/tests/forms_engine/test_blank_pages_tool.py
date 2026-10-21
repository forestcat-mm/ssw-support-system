"""tools/forms/blank_pages.py（空の様式の頁数・字体・代わりの字体の調べ）の、変換を使わない部分。"""

import sys
from collections import Counter
from pathlib import Path

from django.conf import settings

TOOLS = Path(settings.FORM_TEMPLATES_ROOT).parent.parent / "tools" / "forms"
sys.path.insert(0, str(TOOLS))
import blank_pages  # type: ignore[import-not-found]  # noqa: E402

ROOT = Path(settings.FORM_TEMPLATES_ROOT)
FONTS = blank_pages.Fonts(
    blank_pages.read_rules(), {blank_pages.key("IPAMincho"), blank_pages.key("Inter")}
)


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


def test_fallbacks_skip_chars_drawn_in_the_expected_font() -> None:
    requested = {
        "日": Counter({"ＭＳ 明朝": 2}),
        "1": Counter({"Century": 1}),
        "⑴": Counter({"ＭＳ 明朝": 1}),
    }
    embedded = Counter({("IPAMincho", "日"): 2, ("Inter", "1"): 1, ("WenQuanYiZenHei", "⑴"): 1})
    embedded[("Inter", "※")] = 1
    found = blank_pages.fallbacks(requested, embedded, FONTS)
    assert {(fb.requested, fb.chosen, fb.reason): dict(chars) for fb, chars in found.items()} == {
        ("Century", "Inter", "規則が無い"): {"1": 1},
        ("ＭＳ 明朝", "WenQuanYiZenHei", "置き換え先に字が無い"): {"⑴": 1},
        ("（文書の文字に無い）", "Inter", "文書の文字に無い"): {"※": 1},
    }


def test_docx_chars_take_the_font_from_runs_styles_and_theme() -> None:
    chars = blank_pages.char_fonts(ROOT / "13-1" / "2026-10-18" / "original.docx")
    # 数字は asciiTheme="minorEastAsia"（テーマの日本語の字体）か、既定の minorHAnsi（Century）
    assert set(chars["5"]) <= {"ＭＳ 明朝［テーマ］", "Century［テーマ］"}
    assert "ＭＳ 明朝［テーマ］" in chars["製"]


def test_xlsx_chars_take_the_font_of_the_cell() -> None:
    chars = blank_pages.char_fonts(ROOT / "1-17" / "2026-10-17.zh-hans" / "original.xlsx")
    assert chars and all(
        set(fonts) <= {"Noto Sans CJK SC", "Noto Serif CJK SC"}
        for char, fonts in chars.items()
        if blank_pages.is_east_asian(char)
    )
