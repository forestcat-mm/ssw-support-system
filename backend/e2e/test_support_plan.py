"""通しのテスト: 支援計画（T17）。標準から作る → 1 か所変えて新版 → 差分が見える。"""

import pytest
from playwright.sync_api import Page, expect

from apps.core.models import User
from apps.workers.models import ForeignWorker
from e2e.conftest import SignIn
from tests.contracts.factories import make_contract, site_with_categories
from tests.core.factories import make_staff
from tests.workers.factories import make_worker


@pytest.fixture
def worker() -> ForeignWorker:
    worker = make_worker(assigned_staff=make_staff(name="佐藤 花子", position="支援担当"))
    make_contract(worker, site=site_with_categories())
    return worker


@pytest.mark.parametrize("width", [1366, 390])
def test_create_plan_then_add_version_and_see_diff(
    page: Page, sign_in: SignIn, support_staff: User, worker: ForeignWorker, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(support_staff)
    base = page.url.rstrip("/")
    page.goto(f"{base}/workers/{worker.pk}/")
    page.get_by_role("navigation", name="区分").get_by_role("link", name="支援").click()
    page.get_by_role("link", name="支援計画を作成").first.click()

    # 標準から複製され、担当者が補われた状態で開く（区分はたたまれている）
    expect(page.get_by_role("heading", level=1)).to_have_text("支援計画を作成")
    expect(page.locator("#section-1")).not_to_have_attribute("open", "")
    page.get_by_role("button", name="保存する").click()
    expect(page.get_by_text("支援計画を作成しました。")).to_be_visible()
    expect(page.locator("#plan-version-label")).to_contain_text("第1版")
    page.locator("#section-1 > summary").click()
    expect(page.locator("#section-1")).to_contain_text("佐藤 花子（支援担当）")

    # 1 か所（相談の実施時期）を変えて新版を足す
    page.get_by_role("link", name="新しい版を追加").click()
    page.locator("#section-6 > summary").click()
    page.locator("#id_act-6_main-planned_note").fill("平日9時〜18時に随時")
    page.get_by_label("変更理由").fill("受付時間の見直し")
    page.get_by_role("button", name="保存する").click()

    expect(page.get_by_text("第2版を追加しました。")).to_be_visible()
    diff = page.locator("#plan-diff")
    expect(diff).to_contain_text("第1版から第2版への変更")
    expect(diff).to_contain_text("平日9時〜18時に随時")
    assert page.evaluate("document.documentElement.scrollWidth") <= width


def test_support_staff_cannot_open_standard_plan(
    page: Page, sign_in: SignIn, support_staff: User
) -> None:
    sign_in(support_staff)
    response = page.goto(page.url.rstrip("/") + "/settings/support-plan/")
    assert response is not None and response.status == 403
