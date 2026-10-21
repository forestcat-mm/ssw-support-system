"""通しのテスト: 外国人の詳細の上の帯・細い帯・目次・概要（U3。仕様 5.2・7、見本 03・04）。"""

from datetime import timedelta
from urllib.parse import urlparse

import pytest
from django.utils import timezone
from playwright.sync_api import Browser, Page, Request, expect
from pytest_django.live_server_helper import LiveServer

from apps.core.models import User
from apps.filings.models import FilingKind, Trigger
from apps.workers.models import ForeignWorker
from e2e.conftest import SignIn
from services import filings
from tests.contracts.factories import make_contract
from tests.workers.factories import add_passport, add_residence, make_worker


@pytest.fixture
def worker() -> ForeignWorker:
    today = timezone.localdate()
    worker = make_worker()
    add_residence(worker, card_number="AB12345678CD", expires_on=today - timedelta(days=5))
    add_passport(worker, number="E12345678")
    contract = make_contract(worker)
    filings.create(
        FilingKind.CONTRACT_CHANGE,
        target=contract,
        basis_on=today - timedelta(days=20),
        trigger=Trigger.MANUAL,
    )
    return worker


@pytest.mark.parametrize("width", [1366, 390])
def test_header_toc_and_the_bar_on_scroll(
    page: Page, sign_in: SignIn, support_staff: User, worker: ForeignWorker, width: int
) -> None:
    admin_requests: list[str] = []

    def watch(request: Request) -> None:
        if urlparse(request.url).path.startswith("/admin/"):
            admin_requests.append(request.url)

    page.on("request", watch)
    page.set_viewport_size({"width": width, "height": 800})
    sign_in(support_staff)
    page.goto(page.url.rstrip("/") + f"/workers/{worker.pk}/")

    head = page.locator("#person-head")
    expect(head.get_by_role("heading", level=1)).to_have_text("王芳")
    expect(page.locator("#residence-expiry")).to_contain_text("超過 5日")
    expect(page.locator("#todo [data-todo]").first).to_contain_text("雇用契約変更届出")
    expect(page.locator("#person-bar")).to_be_hidden()

    # 目次で移ると、ふだんの帯が画面の外に出て細い帯が出る。今いるセクションに aria-current
    toc = page.get_by_role("navigation", name="このページの目次")
    expect(toc.locator('a[href="#residence"] .toc__warn')).to_have_count(1)  # 注意の点
    toc.get_by_role("link", name="5 雇用契約").click()
    bar = page.locator("#person-bar")
    expect(bar).to_be_visible()
    expect(bar.locator('[data-toc-target="contract"]')).to_have_attribute(
        "aria-current", "location"
    )
    # 移った先のセクションの見出しは、細い帯に隠れない
    heading = page.locator("#contract-title").bounding_box()
    bar_box = bar.bounding_box()
    assert heading is not None and bar_box is not None
    assert heading["y"] >= bar_box["y"] + bar_box["height"] - 1

    # 一番上に戻ると細い帯は消える
    page.evaluate("window.scrollTo(0, 0)")
    expect(bar).to_be_hidden()
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    assert admin_requests == []


def test_without_javascript_the_bar_is_not_shown(
    page: Page,
    browser: Browser,
    live_server: LiveServer,
    sign_in: SignIn,
    support_staff: User,
    worker: ForeignWorker,
) -> None:
    """JS が動かないときは、ふだんの帯と本体の上の目次だけが出る（仕様 7）。"""
    sign_in(support_staff)
    context = browser.new_context(
        java_script_enabled=False, storage_state=page.context.storage_state()
    )
    plain = context.new_page()
    plain.goto(f"{live_server.url}/workers/{worker.pk}/")
    expect(plain.locator("#person-head")).to_be_visible()
    expect(plain.get_by_role("navigation", name="このページの目次")).to_be_visible()
    plain.mouse.wheel(0, 2000)
    expect(plain.locator("#person-bar")).to_be_hidden()
    context.close()
