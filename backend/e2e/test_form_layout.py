"""通しのテスト: 入力の画面の型（U6。仕様 4・5.3・見本 07）。

外国人の基本情報の編集で、上に細い帯、下に留める帯（入力が足りない項目の数）、欄を離れたときの必須の確かめ。
雇用契約の新しい版で、番号つきのセクションと細い帯の中の入力の目次。横にはみ出さず、管理画面へ行かない。
"""

from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Request, expect

from apps.core.models import User
from apps.workers.models import ForeignWorker
from e2e.conftest import SignIn
from tests.contracts.factories import make_contract
from tests.workers.factories import make_worker


@pytest.fixture
def worker() -> ForeignWorker:
    worker = make_worker()
    make_contract(worker)
    return worker


@pytest.mark.parametrize("width", [1366, 390])
def test_band_bottom_bar_and_required_check(
    page: Page, sign_in: SignIn, administrator: User, worker: ForeignWorker, width: int
) -> None:
    admin_requests: list[str] = []

    def watch(request: Request) -> None:
        if urlparse(request.url).path.startswith("/admin/"):
            admin_requests.append(request.url)

    page.on("request", watch)
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(administrator)
    base = page.url.rstrip("/")
    page.goto(f"{base}/workers/{worker.pk}/edit/")
    band = page.locator("#person-band")
    expect(band).to_be_visible()
    expect(band.get_by_role("link", name="外国人の詳細へ戻る")).to_be_visible()

    # 必須の欄を空にして離れると、欄の下に知らせ、下の帯に件数。押すとその欄へ
    missing = page.locator(".form-bar__missing")
    expect(missing).to_be_hidden()
    name = page.locator("#id_name_romaji")
    name.fill("")
    name.blur()
    expect(page.locator("#id_name_romaji ~ [role='alert']")).to_have_text("入力してください。")
    expect(missing).to_have_text("入力が足りない項目 1 件")
    page.locator("#id_name_kana").focus()
    missing.click()
    expect(name).to_be_focused()
    name.fill("WANG FANG")
    expect(missing).to_be_hidden()
    expect(page.locator("#id_name_romaji ~ [role='alert']")).to_have_count(0)

    # 下の帯は画面の下に留まる（「保存する」は 1 つだけ）
    bar = page.locator(".form-bar")
    box = bar.bounding_box()
    assert box is not None and box["y"] + box["height"] <= 900 + 1
    expect(page.get_by_role("button", name="保存する")).to_have_count(1)
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.get_by_role("button", name="保存する").click()
    expect(page.get_by_text("WANG FANG").first).to_be_visible()
    assert admin_requests == []


@pytest.mark.parametrize("width", [1366, 390])
def test_contract_form_sections_and_toc(
    page: Page, sign_in: SignIn, administrator: User, worker: ForeignWorker, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(administrator)
    base = page.url.rstrip("/")
    contract = worker.employment_contracts.get()
    page.goto(f"{base}/workers/{worker.pk}/contracts/{contract.pk}/versions/new/")
    toc = page.get_by_role("navigation", name="入力の目次")
    expect(toc).to_be_visible()
    links = toc.get_by_role("link")
    assert links.count() >= 4
    expect(links.first).to_have_text("1 変更")
    # 変更日・変更理由（必須）が空なので、最初のセクションに足りない印
    expect(links.first).to_have_class("toc__link toc__link--danger")
    links.nth(2).click()
    target = links.nth(2).get_attribute("href")
    assert target
    expect(page.locator(target)).to_be_in_viewport()
    assert page.evaluate("document.documentElement.scrollWidth") <= width
