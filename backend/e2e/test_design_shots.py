"""デザインの比較セットのスクリーンショット（T13a）。make design-shots のときだけ動く。

5 セット × PC 幅（1366px）と 390px 幅のページ全体を、1 倍解像度で docs/design/ に保存する。
"""

import os
from pathlib import Path

import pytest
from playwright.sync_api import Page

from apps.core.models import User
from e2e.conftest import SignIn

OUTPUT = Path(__file__).resolve().parents[2] / "docs" / "design"
WIDTHS = {"desktop": 1366, "mobile": 390}

pytestmark = pytest.mark.skipif(
    os.environ.get("DESIGN_SHOTS") != "1", reason="make design-shots のときだけ撮る"
)


@pytest.mark.parametrize("theme", ["a", "b", "c", "d", "e"])
def test_capture_design_set(page: Page, sign_in: SignIn, administrator: User, theme: str) -> None:
    sign_in(administrator)
    base = page.url.rstrip("/")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, width in WIDTHS.items():
        page.set_viewport_size({"width": width, "height": 900})
        page.goto(f"{base}/ui/?theme={theme}")
        page.screenshot(path=OUTPUT / f"{theme}-{name}.png", full_page=True, scale="css")
