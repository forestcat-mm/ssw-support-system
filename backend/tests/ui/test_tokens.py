"""デザイントークンと字体（U1。ADR-0046）: 文字と背景の対比、字体の同梱、外から読まないこと。"""

import re
from pathlib import Path

import pytest
from django.test import Client

STATIC_UI = Path(__file__).resolve().parents[2] / "apps" / "core" / "static" / "ui"

pytestmark = pytest.mark.django_db


def palette() -> dict[str, str]:
    text = (STATIC_UI / "tokens.css").read_text()
    pattern = r"^\s*(--palette-[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{6})"
    return dict(re.findall(pattern, text, re.MULTILINE))


def luminance(hex_color: str) -> float:
    channels = []
    for i in (1, 3, 5):
        c = int(hex_color[i : i + 2], 16) / 255
        channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg: str, bg: str) -> float:
    a, b = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


# 文字と背景の組み合わせ（仕様 7: 補足の 12px も 4.5:1 以上）
PAIRS = [
    ("--palette-text", "--palette-white"),
    ("--palette-text", "--palette-bg"),
    ("--palette-text-2", "--palette-white"),
    ("--palette-text-sub", "--palette-white"),
    ("--palette-text-sub", "--palette-bg"),
    ("--palette-text-sub", "--palette-head"),
    ("--palette-text-sub", "--palette-group"),
    ("--palette-accent", "--palette-white"),
    ("--palette-accent", "--palette-accent-soft"),
    ("--palette-white", "--palette-accent"),
    ("--palette-danger", "--palette-white"),
    ("--palette-danger", "--palette-danger-soft"),
    ("--palette-danger-count-text", "--palette-danger-count"),
    ("--palette-caution", "--palette-white"),
    ("--palette-caution", "--palette-caution-soft"),
    ("--palette-ok", "--palette-white"),
    ("--palette-ok", "--palette-ok-soft"),
]


@pytest.mark.parametrize(("fg", "bg"), PAIRS)
def test_text_contrast_is_at_least_4_5(fg: str, bg: str) -> None:
    colors = palette()
    assert contrast(colors[fg], colors[bg]) >= 4.5, (fg, bg)


def test_fonts_are_bundled() -> None:
    css = (STATIC_UI / "fonts.css").read_text()
    urls = re.findall(r'url\("([^"]+)"\)', css)
    assert urls, "字体の @font-face が無い"
    for url in urls:
        assert not url.startswith(("http:", "https:", "//")), url
        assert (STATIC_UI / url).is_file(), url
    assert {"400", "500", "700"} <= set(re.findall(r"NotoSansJP-(\d+)\.woff2", css))
    assert (STATIC_UI / "fonts" / "OFL.txt").read_text().startswith("Copyright")


def test_business_layout_reads_nothing_from_outside(support_staff_client: Client) -> None:
    html = support_staff_client.get("/").content.decode()
    assert '<link rel="stylesheet" href="/static/ui/fonts.css">' in html
    for tag in re.findall(r"<(?:link|script|img)[^>]+>", html):
        assert not re.search(r'(?:href|src)="(?:https?:)?//', tag), tag
    for css in ("tokens.css", "base.css", "components.css", "fonts.css"):
        assert "fonts.googleapis.com" not in (STATIC_UI / css).read_text()
