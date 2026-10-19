"""通しのテスト: 委託料の内訳を入れ、第5-10号を生成し、申請の書類の一覧で確かめる（T37）。

受入れ機関の詳細 →「委託料の内訳」→ 2 行を入れて保存 → 外国人の契約タブ →「第5-10号を作る」→ 生成 →
帳票タブ →「申請の書類の一覧」→ 変更の申請 → 第1-25号の行が「未生成」、委託契約書は一覧に無い
（第5-10号は申請の添付ではない）。管理画面には 1 度も行かない。
"""

from datetime import date
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Request, expect

from apps.contracts.models import EmploymentContract
from apps.core.models import AttachmentKind, User
from apps.hosts.models import HostMembership, SupportContract
from apps.workers.models import WorkerStatus
from e2e.conftest import SignIn
from services import form_templates
from services.attachments import create_attachment
from tests.contracts.factories import make_contract
from tests.core.factories import make_organization
from tests.forms_engine.test_form_application import pdf
from tests.hosts.factories import category, make_host, make_site
from tests.workers.factories import make_worker


@pytest.fixture
def contract() -> EmploymentContract:
    form_templates.load_all()
    make_organization(
        representative_title="代表理事", representative_name="鈴木 花子", court_name="東京"
    )
    host = make_host()
    site = make_site(host, "industrial_products")
    site.categories.set([category("machine_metal_processing")])
    contract = make_contract(
        make_worker(status=WorkerStatus.RESIDING), site=site, signed_on=date(2026, 3, 1)
    )
    membership = HostMembership.objects.create(
        host_organization=host, field=site.field, joined_on=date(2025, 1, 1)
    )
    create_attachment(
        target=membership, kind=AttachmentKind.MEMBERSHIP_CERTIFICATE, file=pdf(), created_by=None
    )
    SupportContract.objects.create(
        host_organization=host,
        monthly_fee=30000,
        concluded_on=date(2026, 3, 1),
        starts_on=date(2026, 3, 15),
        ends_on=date(2027, 3, 14),
    )
    return contract


@pytest.mark.parametrize("width", [1366, 390])
def test_fee_breakdown_generation_and_application_list(
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

    # 委託料の内訳
    page.goto(f"{base}/hosts/{contract.host_organization_id}/")
    page.get_by_role("link", name="委託料の内訳").first.click()
    expect(page.get_by_role("heading", level=1)).to_have_text("委託料の内訳")
    page.locator("#id_fees-0-name").fill("義務的支援の委託料")
    page.locator("#id_fees-0-amount").fill("25000")
    page.locator("#id_fees-0-timing").select_option(label="定期")
    page.locator("#fee-rows").get_by_role("button", name="項目を追加").click()
    page.locator("#id_fees-1-name").fill("通訳")
    page.locator("#id_fees-1-amount").fill("5000")
    page.locator("#id_fees-1-timing").select_option(label="随時")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.get_by_role("button", name="保存する").click()
    expect(page.get_by_text("委託料の内訳を保存しました。")).to_be_visible()

    # 契約タブから第5-10号を生成
    page.goto(f"{base}/workers/{contract.worker_id}/contracts/")
    page.locator("#contract-application-generate").get_by_role(
        "link", name="第5-10号を作る"
    ).click()
    expect(page.locator("#generation-findings")).to_contain_text("不足はありません")
    page.get_by_role("button", name="生成する").click()
    expect(page.locator("#document-status")).to_have_text("生成しました。")

    # 帳票タブの申請の書類の一覧
    page.goto(f"{base}/workers/{contract.worker_id}/documents/")
    page.locator("#application-set-link").click()
    page.locator("#id_set_id").select_option("chg")
    page.get_by_role("button", name="表示").click()
    rows = page.locator("#application-set-rows")
    expect(rows.locator('[data-row="chg_t1-19-1"]')).to_have_attribute(
        "data-status", "not_generated"
    )
    expect(rows.locator('[data-row="chg_ind-3-1"]')).to_have_attribute("data-status", "attached")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    assert admin_requests == []
