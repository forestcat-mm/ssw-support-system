"""通しのテスト: キャリアアッププランを作り、契約タブから生成して、交付記録を作る（T36）。

受入れ機関の詳細 →「キャリアアッププランを作る」→ 保存 → 外国人の契約タブ →「キャリアアッププランを
作る」→ 生成 → 生成物の画面の「写しの交付を記録する」→ 契約が入った交付の画面 → 保存。
管理画面には 1 度も行かない。
"""

from datetime import date
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Request, expect

from apps.contracts.models import EmploymentContract
from apps.core.models import User
from apps.workers.models import WorkerStatus
from e2e.conftest import SignIn
from services import form_templates
from tests.contracts.factories import make_contract
from tests.hosts.factories import category, make_host, make_site
from tests.workers.factories import make_worker


@pytest.fixture
def contract() -> EmploymentContract:
    form_templates.load_all()
    host = make_host()
    site = make_site(host, "food_manufacturing", name="食品工場")
    site.categories.set([category("food_manufacturing_general")])
    contract = make_contract(
        make_worker(status=WorkerStatus.RESIDING), site=site, signed_on=date(2026, 3, 1)
    )
    contract.versions.get().categories.set([category("food_manufacturing_general")])
    return contract


@pytest.mark.parametrize("width", [1366, 390])
def test_create_generate_and_deliver_the_career_plan(
    page: Page, sign_in: SignIn, support_staff: User, contract: EmploymentContract, width: int
) -> None:
    admin_requests: list[str] = []

    def watch(request: Request) -> None:
        if urlparse(request.url).path.startswith("/admin/"):
            admin_requests.append(request.url)

    page.on("request", watch)
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(support_staff)
    base = page.url.rstrip("/")

    # 受入れ機関の詳細からプランを作る
    page.goto(f"{base}/hosts/{contract.host_organization_id}/")
    page.locator("#career-plan-create").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("キャリアアッププランを作る")
    page.locator("#id_field").select_option(label="飲食料品製造業分野")
    page.locator("#id_applies_from").fill("2026-01-01")
    page.locator("#id_career_route").fill("作業員 → ライン長補佐 → ライン長")
    page.locator("#level-rows").get_by_role("button", name="レベルを足す").click()
    page.locator("#id_levels-0-name").fill("レベル1")
    page.locator("#id_levels-0-years").fill("1〜2年目")
    page.locator("#id_levels-0-duties").fill("製造ラインの基本作業")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.get_by_role("button", name="保存する").click()
    expect(page.locator("#career-plan-levels")).to_contain_text("製造ラインの基本作業")

    # 契約タブから生成
    page.goto(f"{base}/workers/{contract.worker_id}/contracts/")
    page.locator("#contract-generate").get_by_role(
        "link", name="キャリアアッププランを作る"
    ).click()
    expect(page.locator("#generation-findings")).to_contain_text("不足はありません")
    page.get_by_role("button", name="生成する").click()
    expect(page.locator("#document-status")).to_have_text("生成しました。")
    assert page.evaluate("document.documentElement.scrollWidth") <= width

    # 生成物から交付記録を作る（契約が入っている）
    page.locator("#document-deliver").click()
    expect(page.locator("#id_contract")).to_have_value(str(contract.pk))
    page.locator("#id_delivered_on").fill("2026-02-20")
    page.locator("#id_explained").check()
    page.get_by_role("button", name="保存する").click()
    expect(page.get_by_text("交付記録を保存しました。")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    assert admin_requests == []
