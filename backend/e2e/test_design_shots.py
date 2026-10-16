"""見本画面のスクリーンショット（T13a）。make design-shots のときだけ動く。

PC 幅（1366px）と 390px 幅のページ全体を、1 倍解像度で docs/design/ に保存する。
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


def test_capture_ui_components(page: Page, sign_in: SignIn, administrator: User) -> None:
    sign_in(administrator)
    base = page.url.rstrip("/")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, width in WIDTHS.items():
        page.set_viewport_size({"width": width, "height": 900})
        page.goto(f"{base}/ui/")
        page.screenshot(path=OUTPUT / f"{name}.png", full_page=True, scale="css")
