"""通しのテスト: 外国人一覧（T13b）。"""

from datetime import timedelta
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.utils import timezone
from playwright.sync_api import Page, expect
from pytest_django.fixtures import Settings

from apps.core.models import Country, User
from e2e.conftest import SignIn
from tests.workers.factories import add_residence, make_worker

ROW_HEIGHTS = "rows => rows.map(row => row.getBoundingClientRect().height)"


@pytest.fixture
def mixed_workers() -> None:
    """漢字・簡体字・ラテン文字（声調記号つき）・ローマ字だけの氏名が混ざった 4 人。"""
    today = timezone.localdate()
    wang = make_worker(name_romaji="WANG FANG", name_kanji="王芳", name_native="王芳")
    add_residence(wang, card_number="AB12345678CD", expires_on=today + timedelta(days=3))
    li = make_worker(name_romaji="LI NA", name_kanji="", name_native="李娜")
    add_residence(li, card_number="AB22222222CD", expires_on=today + timedelta(days=12))
    nguyen = make_worker(
        name_romaji="NGUYEN VAN AN",
        name_kanji="",
        name_native="Nguyễn Văn An",
        nationality=Country.objects.get(code="VN"),
    )
    add_residence(nguyen, card_number="AB33333333CD", expires_on=today + timedelta(days=60))
    siti = make_worker(
        name_romaji="SITI RAHAYU",
        name_kanji="",
        name_native="",
        nationality=Country.objects.get(code="ID"),
    )
    add_residence(siti, card_number="AB44444444CD", expires_on=today + timedelta(days=400))


@pytest.mark.usefixtures("mixed_workers")
def test_open_list_from_navigation_and_search_by_card_number(
    page: Page, sign_in: SignIn, support_staff: User
) -> None:
    sign_in(support_staff)
    page.get_by_role("navigation", name="メインメニュー").get_by_role(
        "link", name="外国人台帳"
    ).first.click()
    expect(page.get_by_role("heading", level=1)).to_have_text("外国人台帳")
    rows = page.locator("#worker-list tbody tr")
    expect(rows).to_have_count(4)
    expect(page.locator('[data-level-count="within_7"]')).to_have_text("7日以内 1人")

    # 番号は表記ゆれ（小文字・空白）を吸収して完全一致で探す
    page.get_by_label("検索").fill("ab1234 5678cd")
    page.get_by_role("button", name="絞り込む").click()
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("王芳")
    expect(page.get_by_role("link", name="条件を消す")).to_be_visible()


@pytest.mark.usefixtures("mixed_workers")
@pytest.mark.parametrize("width", [1366, 390])
def test_rows_have_the_same_height_for_any_script(
    page: Page, sign_in: SignIn, support_staff: User, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(support_staff)
    page.goto(page.url.rstrip("/") + "/workers/")
    rows = page.locator("#worker-list tbody tr")
    expect(rows).to_have_count(4)
    expect(page.locator('#worker-list [lang="zh-Hans"]')).to_have_text("李娜")
    heights = rows.evaluate_all(ROW_HEIGHTS)
    assert max(heights) - min(heights) < 0.5, heights
    assert page.evaluate("document.documentElement.scrollWidth") <= width


@pytest.fixture
def dummy_data(settings: Settings, tmp_path: Path) -> None:
    """seed_dummy の 30 人（受入れ機関名・担当者名が長い実際に近いデータ）。"""
    settings.DEBUG = True
    settings.ALLOW_DUMMY_DATA = True
    settings.STORAGES = {
        **settings.STORAGES,
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path / "media")},
        },
    }
    call_command("seed_dummy", stdout=StringIO())


@pytest.mark.usefixtures("dummy_data")
@pytest.mark.parametrize("width", [1366, 390])
def test_dummy_30_workers_fit_one_line_per_row(
    page: Page, sign_in: SignIn, support_staff: User, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(support_staff)
    page.goto(page.url.rstrip("/") + "/workers/?status=all")
    rows = page.locator("#worker-list tbody tr")
    expect(rows).to_have_count(30)
    heights = rows.evaluate_all(ROW_HEIGHTS)
    assert max(heights) - min(heights) < 0.5, heights
    assert 34 <= heights[0] <= 40, heights[0]
    # 省略しても title で全文が読める
    host = page.locator("#worker-list .truncate-line--wide").first
    assert "株式会社" in (host.get_attribute("title") or "")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    if width >= 1366:
        # PC 幅では表の全列が見える（表の中の横スクロールも出ない）
        wrap = page.locator(".table-wrap")
        assert wrap.evaluate("element => element.scrollWidth <= element.clientWidth")
