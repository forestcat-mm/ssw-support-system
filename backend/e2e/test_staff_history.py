"""通しのテスト: 担当者を変える（T45）。外国人詳細の「担当者を変える」で新しい担当者・開始日・理由を
入れると、履歴の行が足され、今の担当者が変わる。誤った行を取り消すと前の担当者に戻る。推定の行には
「推定」の印が出る。管理画面には行かない。"""

from datetime import date
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Request, expect

from apps.core.models import User
from apps.workers.models import ForeignWorker, StaffAssignment
from e2e.conftest import SignIn
from tests.core.factories import make_staff
from tests.workers.factories import make_worker


@pytest.fixture
def worker() -> ForeignWorker:
    worker = make_worker(assigned_staff=make_staff(name="佐藤 花子"))
    make_staff(name="鈴木 次郎")
    StaffAssignment.objects.create(
        worker=worker, staff=worker.assigned_staff, starts_on=date(2026, 4, 1), is_estimated=True
    )
    return worker


def watch_admin(page: Page) -> list[str]:
    seen: list[str] = []

    def watch(request: Request) -> None:
        if urlparse(request.url).path.startswith("/admin/"):
            seen.append(request.url)

    page.on("request", watch)
    return seen


@pytest.mark.parametrize("width", [1366, 390])
def test_change_and_void_the_assigned_staff(
    page: Page, sign_in: SignIn, support_staff: User, worker: ForeignWorker, width: int
) -> None:
    admin_requests = watch_admin(page)
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(support_staff)
    base = page.url.rstrip("/")
    page.goto(f"{base}/workers/{worker.pk}/")
    expect(page.locator("#worker-assigned-staff")).to_contain_text("佐藤 花子")
    page.locator("#staff-change-link").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("担当者を変える")
    expect(page.locator("#current-staff")).to_have_text("佐藤 花子")
    page.get_by_label("新しい担当者").select_option(label="鈴木 次郎")
    page.get_by_label("変更の理由").fill("異動のため引継ぎ")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.get_by_role("button", name="変更").click()

    expect(page.get_by_text("担当者を変えました。")).to_be_visible()
    rows = page.locator("#staff-period-table tbody tr")
    expect(rows).to_have_count(2)
    expect(rows.first).to_contain_text("鈴木 次郎")
    expect(rows.last.locator("[data-estimated]")).to_have_text("推定")
    assert page.evaluate("document.documentElement.scrollWidth") <= width

    rows.first.get_by_role("link", name="取り消す").click()
    page.get_by_label("取り消しの理由").fill("別の外国人と取り違えた")
    page.get_by_role("button", name="取り消す").click()
    expect(page.get_by_text("取り消しました。").first).to_be_visible()
    expect(page.locator("#staff-period-table tbody tr.is-voided")).to_have_count(1)
    page.goto(f"{base}/workers/{worker.pk}/")
    expect(page.locator("#worker-assigned-staff")).to_contain_text("佐藤 花子")
    assert admin_requests == []
