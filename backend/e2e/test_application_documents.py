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

    # 委託料の内訳は支援委託契約の画面の委託料の欄からモーダルで入れる（修正 1 回目 B11）
    page.goto(f"{base}/hosts/{contract.host_organization_id}/")
    page.locator("#support-contracts").get_by_role("link", name="編集").first.click()
    expect(page.locator("[data-fee-summary]")).to_have_text("内訳なし。")
    page.get_by_role("button", name="内訳", exact=True).click()
    dialog = page.get_by_role("dialog", name="委託料の内訳")
    expect(dialog).to_be_visible()
    expect(dialog.locator("[data-fee-amount]")).to_have_text("30,000")
    page.locator("#fee-rows").get_by_role("button", name="項目を追加").click()
    page.locator("#id_fees-0-name").fill("義務的支援の委託料")
    page.locator("#id_fees-0-amount").fill("25000")
    page.locator("#id_fees-0-timing").select_option(label="定期")
    page.locator("#fee-rows").get_by_role("button", name="項目を追加").click()
    page.locator("#id_fees-1-name").fill("通訳")
    page.locator("#id_fees-1-amount").fill("4000")
    page.locator("#id_fees-1-timing").select_option(label="随時")
    expect(dialog.locator("[data-fee-mismatch]")).to_be_visible()  # 入力中の知らせ
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    # 合わないまま閉じると選ぶ: 戻って直す
    dialog.get_by_role("button", name="完了").click()
    choice = dialog.locator("[data-fee-choice]")
    expect(choice).to_be_visible()
    choice.get_by_role("button", name="戻って内訳を直す").click()
    page.locator("#id_fees-1-amount").fill("6000")
    dialog.get_by_role("button", name="完了").click()
    # 委託料を内訳の合計に直す
    choice.get_by_role("button", name="委託料を内訳の合計に直す").click()
    expect(dialog).to_be_hidden()
    expect(page.locator("#id_monthly_fee")).to_have_value("31000")
    expect(page.locator("[data-fee-summary]")).to_have_text("内訳 2 件・合計 31,000 円")
    # 内訳を直して委託料を戻す
    page.locator("#id_monthly_fee").fill("30000")
    page.get_by_role("button", name="内訳", exact=True).click()
    page.locator("#id_fees-1-amount").fill("5000")
    expect(dialog.locator("[data-fee-mismatch]")).to_be_hidden()
    dialog.get_by_role("button", name="完了").click()
    expect(dialog).to_be_hidden()
    page.get_by_role("button", name="保存する").click()
    expect(page.get_by_text("保存しました。", exact=True)).to_be_visible()
    support = contract.host_organization.support_contracts.get()
    assert support.monthly_fee == 30000
    assert [(fee.name, fee.amount) for fee in support.fees.order_by("order")] == [
        ("義務的支援の委託料", 25000),
        ("通訳", 5000),
    ]

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


@pytest.mark.parametrize("width", [1366, 390])
def test_application_list_generates_and_attaches_in_a_modal(
    page: Page, sign_in: SignIn, support_staff: User, contract: EmploymentContract, width: int
) -> None:
    """申請の書類の一覧: 状態が先頭の列で行の色が分かれ、生成と健康診断の添付はモーダルで行い、
    一覧に戻る（修正 1 回目 B8・B9・B10）。"""
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
    list_url = (
        f"{base}/workers/{contract.worker_id}/documents/application/?set_id=chg&basis_on=2026-10-18"
    )
    page.goto(list_url)
    rows = page.locator("#application-set-rows")
    expect(rows.locator("thead th").first).to_have_text("状態")
    pledge = rows.locator('[data-row="chg_ind-2-1"]')  # 第3-1号
    health = rows.locator('[data-row="chg_t1-8-1"]')  # 健康診断個人票
    expect(pledge).to_have_attribute("data-tone", "danger")
    expect(health).to_have_attribute("data-status", "missing")
    assert page.evaluate("document.documentElement.scrollWidth") <= width

    # 生成: モーダルで検査の結果を見て生成し、一覧に戻る
    pledge.get_by_role("link", name="生成する").click()
    dialog = page.locator("#modal")
    expect(dialog).to_be_visible()
    expect(dialog.locator("#modal-title")).to_contain_text("第3-1号")
    expect(dialog.locator("#generation-findings")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    dialog.get_by_role("link", name="キャンセル").click()
    expect(dialog).to_be_hidden()
    pledge.get_by_role("link", name="生成する").click()
    dialog.locator("#generate-form").get_by_role("button", name="生成する").click()
    expect(page.get_by_text("帳票の生成を依頼しました。")).to_be_visible()
    assert page.url == list_url
    expect(pledge).to_have_attribute("data-status", "generated")
    expect(pledge).to_have_attribute("data-tone", "ok")

    # 健康診断の添付: 種類が選ばれたモーダル。受診日を入れずに送ると、モーダルの中に誤りが出る
    health.get_by_role("link", name="添付").click()
    expect(dialog).to_be_visible()
    expect(dialog.locator("#id_kind")).to_have_value("health_check_record")
    dialog.locator("#id_uploads").set_input_files(
        {"name": "健康診断.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4\n%%EOF\n"}
    )
    dialog.get_by_role("button", name="追加", exact=True).click()
    expect(dialog.locator("#id_examined_on-error")).to_be_visible()
    dialog.locator("#id_examined_on").fill("2026-10-01")
    dialog.locator("#id_uploads").set_input_files(
        {"name": "健康診断.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4\n%%EOF\n"}
    )
    dialog.get_by_role("button", name="追加", exact=True).click()
    expect(page.get_by_text("健康診断の書類を添付しました。")).to_be_visible()
    assert page.url == list_url
    expect(health).to_have_attribute("data-status", "attached")
    expect(health).to_contain_text("2026-10-01")
    assert admin_requests == []
