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

from apps.contracts.models import EmploymentContract
from apps.core.models import User
from apps.hosts.models import HostOrganization
from apps.seed.context import DUMMY_USERNAME
from apps.seed.models import SeedObject
from apps.workers.models import ForeignWorker
from e2e.conftest import SignIn
from services import deadlines, worker_detail
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


def overdue_worker(seeded: list[ForeignWorker]) -> ForeignWorker:
    """在留期限を過ぎた外国人（見本 03 の呉雪麗さんの場面）。"""
    today = timezone.localdate()
    for worker in seeded:
        deadline = worker_detail.basic_info(worker).residence_deadline
        if deadline and deadlines.assess(deadline, today).level is deadlines.Level.OVERDUE:
            return worker
    return seeded[0]


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
        # 03・04: 在留期限が過ぎている外国人の概要（ふだん・目次で 3 旅券へ移った後の画面の高さ）
        page.goto(f"{base}/workers/{overdue_worker(seeded).pk}/")
        shoot(page, "03-worker-overview", width)
        page.get_by_role("navigation", name="このページの目次").get_by_role(
            "link", name="3 旅券"
        ).click()
        page.locator("#person-bar").wait_for(state="visible")
        page.locator('#person-bar [aria-current="location"]').wait_for()
        page.wait_for_timeout(300)
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=SHOTS / f"04-worker-overview-scrolled-{width}.png", scale="css")
        page.evaluate("window.scrollTo(0, 0)")
        worker = overdue_worker(seeded)
        page.goto(f"{base}/workers/{worker.pk}/contracts/")
        shoot(page, "05-worker-contract-tab", width)
        page.goto(f"{base}/workers/{worker.pk}/support/")
        shoot(page, "06-worker-support-tab", width)
        # 07: 雇用契約の新しい版の入力（上の細い帯・番号つきのセクション・下に留める帯。U6）
        contract = EmploymentContract.objects.filter(worker=worker).order_by("-pk").first()
        if contract:
            page.goto(f"{base}/workers/{worker.pk}/contracts/{contract.pk}/versions/new/")
            shoot(page, "07-contract-version-form", width)
        # 08: 申請の書類の一覧（件数の要約・状態の点・操作の列。U7）
        page.goto(f"{base}/workers/{seeded[0].pk}/documents/application/")
        shoot(page, "08-application-documents", width)
        # 受入れ機関の詳細（U7。ふだんと、目次で支援委託契約へ移った後の画面の高さ）
        host = HostOrganization.objects.filter(
            pk__in=SeedObject.objects.filter(content_type__model="hostorganization").values(
                "object_id"
            )
        ).order_by("pk")[0]
        page.goto(f"{base}/hosts/{host.pk}/")
        shoot(page, "09-host-detail", width)
        page.get_by_role("navigation", name="このページの目次").get_by_role(
            "link", name="5 支援委託契約"
        ).click()
        page.locator("#person-bar").wait_for(state="visible")
        page.wait_for_timeout(300)
        page.screenshot(path=SHOTS / f"10-host-detail-scrolled-{width}.png", scale="css")
        page.evaluate("window.scrollTo(0, 0)")
