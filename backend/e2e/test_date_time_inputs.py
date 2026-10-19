"""通しのテスト: 日付と時刻の入力（修正 1 回目 B16。案2）。

日付は文字の欄で、欄を離れると「R8.4.1」などを 2026-04-01 に直して曜日を出す。
横の「カレンダー」で、年・月を選べて「今日」があるカレンダーが開き、日を押すと入る。Esc・外を押すと
閉じる。時刻は 1 つの欄で、「830」を 08:30 に直し、5 分刻みの選択（datalist）も持つ。
1366px と 390px。管理画面には行かない。
"""

from urllib.parse import urlparse

import pytest
from django.utils import timezone
from playwright.sync_api import Page, expect

from apps.core.models import User
from apps.workers.models import ForeignWorker
from e2e.conftest import SignIn
from tests.workers.factories import make_worker


@pytest.fixture
def worker() -> ForeignWorker:
    return make_worker(status="residing")


@pytest.mark.parametrize("width", [1366, 390])
def test_date_and_time_inputs(
    page: Page, sign_in: SignIn, support_staff: User, worker: ForeignWorker, width: int
) -> None:
    admin_requests: list[str] = []
    page.on(
        "request",
        lambda request: (
            admin_requests.append(request.url)
            if urlparse(request.url).path.startswith("/admin/")
            else None
        ),
    )
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(support_staff)
    base = page.url.rstrip("/")
    page.goto(f"{base}/workers/{worker.pk}/contracts/new/")
    starts = page.locator("#id_starts_on")

    # 和暦の短い形を直して、曜日を出す
    starts.fill("R8.4.1")
    starts.press("Tab")
    expect(starts).to_have_value("2026-04-01")
    expect(page.locator("#id_starts_on-note")).to_contain_text("水曜日")

    # カレンダー: 年・月を選び、日を押すと入る
    field = page.locator(".date-field").filter(has=starts)
    field.get_by_role("button", name="カレンダー").click()
    calendar = field.locator(".calendar")
    expect(calendar).to_be_visible()
    selects = calendar.locator("select")
    expect(selects.nth(0)).to_have_value("2026")
    expect(selects.nth(1)).to_have_value("4")
    selects.nth(1).select_option("5")
    calendar.get_by_role("button", name="10", exact=True).click()
    expect(calendar).to_be_hidden()
    expect(starts).to_have_value("2026-05-10")
    box = calendar.bounding_box()
    assert box is None or box["x"] + box["width"] <= width

    # Esc で閉じる・外を押すと閉じる・「今日」
    opener = field.get_by_role("button", name="カレンダー")
    opener.click()
    expect(calendar).to_be_visible()
    open_box = calendar.bounding_box()
    assert (
        open_box is not None and open_box["x"] >= 0 and open_box["x"] + open_box["width"] <= width
    )
    page.keyboard.press("Escape")
    expect(calendar).to_be_hidden()
    expect(opener).to_be_focused()
    opener.click()
    page.locator("h1").click()
    expect(calendar).to_be_hidden()
    opener.click()
    calendar.get_by_role("button", name="今日").click()
    expect(starts).to_have_value(timezone.localdate().isoformat())

    # 時刻: 1 つの欄。「830」を 08:30 に直し、5 分刻みの選択を持つ
    start_time = page.locator("#id_start_time")
    expect(start_time).to_have_attribute("list", "time-options")
    assert page.locator("#time-options option").count() == 288
    start_time.fill("830")
    start_time.press("Tab")
    expect(start_time).to_have_value("08:30")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    assert admin_requests == []
