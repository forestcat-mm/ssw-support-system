"""通しのテスト: 受入れ機関の一覧・詳細と、外国人の無効化（T13e）。"""

import pytest
from playwright.sync_api import Page, expect

from apps.core.models import User
from apps.workers.models import ForeignWorker
from e2e.conftest import SignIn
from tests.contracts.factories import make_contract, site_with_categories
from tests.workers.factories import add_residence, make_worker


@pytest.fixture
def worker() -> ForeignWorker:
    worker = make_worker(status="residing")
    add_residence(worker, card_number="AB12345678CD")
    make_contract(worker, site=site_with_categories())  # テスト工業株式会社
    return worker


def test_host_list_to_detail_to_worker(
    page: Page, sign_in: SignIn, support_staff: User, worker: ForeignWorker
) -> None:
    sign_in(support_staff)
    page.get_by_role("navigation", name="メインメニュー").get_by_role(
        "link", name="受入れ機関"
    ).first.click()
    row = page.locator("#host-list tbody tr").first
    expect(row).to_contain_text("1")  # 所属する外国人
    row.get_by_role("link", name="テスト工業株式会社").click()

    expect(page.get_by_role("heading", level=1)).to_have_text("テスト工業株式会社")
    expect(page.locator("#sites")).to_contain_text("本社工場")
    expect(page.get_by_role("link", name="管理画面で開く")).to_have_count(0)
    page.locator("#host-worker-list").get_by_role("link", name="王芳").click()
    expect(page.get_by_role("heading", level=1)).to_contain_text("王芳")


def test_support_staff_cannot_deactivate(
    page: Page, sign_in: SignIn, support_staff: User, worker: ForeignWorker
) -> None:
    sign_in(support_staff)
    base = page.url.rstrip("/")
    page.goto(f"{base}/workers/{worker.pk}/")
    expect(page.get_by_role("link", name="無効化")).to_have_count(0)
    response = page.goto(f"{base}/workers/{worker.pk}/deactivate/")
    assert response is not None and response.status == 403


def test_admin_deactivates_and_worker_leaves_the_list(
    page: Page, sign_in: SignIn, administrator: User, worker: ForeignWorker
) -> None:
    sign_in(administrator)
    base = page.url.rstrip("/")
    page.goto(f"{base}/workers/{worker.pk}/")
    page.get_by_role("link", name="無効化").click()
    expect(page.locator("#effects")).to_contain_text("特定技能1号")

    page.get_by_role("button", name="無効化する").click()  # 理由が空
    expect(page.locator("#id_reason-error")).to_be_visible()
    page.get_by_label("無効化の理由").fill("誤って二重に登録した")
    page.get_by_role("button", name="無効化する").click()

    expect(page.locator("#deactivated-banner")).to_contain_text("誤って二重に登録した")
    expect(page.get_by_role("link", name="編集")).to_have_count(0)

    page.goto(f"{base}/workers/?status=all")
    expect(page.locator(f'#worker-list tr[data-worker="{worker.pk}"]')).to_have_count(0)
    page.get_by_label("無効化済みを含む").check()  # 変えるとそのまま送る（U2-D4）
    row = page.locator(f'#worker-list tr[data-worker="{worker.pk}"]')
    expect(row).to_contain_text("無効化済み")
