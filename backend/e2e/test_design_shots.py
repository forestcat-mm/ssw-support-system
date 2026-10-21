"""見本画面のスクリーンショット（T13a）。make design-shots のときだけ動く。

PC 幅（1366px）と 390px 幅のページ全体を、1 倍解像度で docs/design/ に保存する。部品の見本（/ui/）は
docs/design/desktop.png・mobile.png、見本 mock/ と同じ画面（seed_dummy のデータ）は
docs/design/design-2026-10/shots/<見本の番号と名前>-<幅>.png。
"""

import os
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.utils import timezone
from playwright.sync_api import Page
from pytest_django.fixtures import Settings

from apps.core.models import User
from apps.seed.context import DUMMY_USERNAME
from apps.seed.models import SeedObject
from apps.workers.models import ForeignWorker
from e2e.conftest import SignIn
from tests.core.factories import TEST_PASSWORD

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


SHOTS = OUTPUT / "design-2026-10" / "shots"


@pytest.fixture
def seeded(settings: Settings, tmp_path: Path) -> list[ForeignWorker]:
    """seed_dummy のデータ（見本の画面と同じ画面を、今のデータで撮る。U8-D1）。"""
    settings.DEBUG = True
    settings.ALLOW_DUMMY_DATA = True
    settings.STORAGES = {
        **settings.STORAGES,
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path / "media")},
        },
    }
    call_command("load_form_templates", stdout=StringIO())
    call_command("seed_dummy", "--date", timezone.localdate().isoformat(), stdout=StringIO())
    user = User.objects.get(username=DUMMY_USERNAME)
    user.set_password(TEST_PASSWORD)
    user.save(update_fields=["password"])
    ids = SeedObject.objects.filter(content_type__model="foreignworker").values("object_id")
    return list(ForeignWorker.objects.filter(pk__in=ids).order_by("pk"))


def shoot(page: Page, name: str, width: int) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=SHOTS / f"{name}-{width}.png", full_page=True, scale="css")


def test_capture_screens_like_the_mocks(
    page: Page, sign_in: SignIn, seeded: list[ForeignWorker]
) -> None:
    """見本 mock/01〜04 と同じ画面（ダッシュボード・外国人台帳・外国人の詳細の概要と

    スクロールした後）。"""
    sign_in(User.objects.get(username=DUMMY_USERNAME))
    base = page.url.rstrip("/")
    for width in WIDTHS.values():
        page.set_viewport_size({"width": width, "height": 900})
        page.goto(f"{base}/")
        shoot(page, "01-dashboard", width)
        page.goto(f"{base}/workers/")
        shoot(page, "02-workers-list", width)
