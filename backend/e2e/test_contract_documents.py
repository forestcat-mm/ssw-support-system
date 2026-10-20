"""通しのテスト: 契約タブの版から、報酬の説明を入れて第1-4号を作り、第1-6号も作る（T34、ADR-0023）。

契約タブ →「報酬の説明を入れる」→ 保存 →「第1-4号を作る」→ 不足なし → 生成 → PDF をダウンロード →
契約タブへ戻る →「第1-6号を作る」→ 生成。管理画面には 1 度も行かない。
"""

from datetime import date
from decimal import Decimal
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Request, expect

from apps.contracts.models import EmploymentContractVersion
from apps.core.models import User
from apps.workers.models import StatusOfResidence
from e2e.conftest import SignIn
from services import form_templates
from tests.contracts.factories import make_contract, site_with_categories
from tests.workers.factories import add_residence, make_worker


@pytest.fixture
def version() -> EmploymentContractVersion:
    form_templates.load_all()
    contract = make_contract(
        make_worker(status="residing"),
        site=site_with_categories(),
        signed_on=date(2026, 3, 1),
        monthly_working_minutes=9800,
        annual_working_minutes=117600,
        hourly_equivalent=1347,
        overtime_rate_within_60h=Decimal("25"),
        treatment_contact_department="総務部",
        treatment_contact_person="総務課長 窓口 一郎",
        treatment_contact_phone="03-0000-0100",
        consultation_contact_department="総務部",
        consultation_contact_person="総務課長 窓口 一郎",
        consultation_contact_phone="03-0000-0100",
        has_deduction_agreement=True,  # 居住費の控除がある
        work_rules_location="事務室に掲示",
    )
    add_residence(contract.worker, status_of_residence=StatusOfResidence.TECHNICAL_INTERN_2)
    return contract.versions.get()


@pytest.mark.parametrize("width", [1366, 390])
def test_generate_contract_documents_from_the_contract_tab(
    page: Page,
    sign_in: SignIn,
    support_staff: User,
    version: EmploymentContractVersion,
    width: int,
) -> None:
    admin_requests: list[str] = []

    def watch(request: Request) -> None:
        if urlparse(request.url).path.startswith("/admin/"):
            admin_requests.append(request.url)

    page.on("request", watch)
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(support_staff)
    base = page.url.rstrip("/")
    worker = version.contract.worker
    page.goto(f"{base}/workers/{worker.pk}/contracts/")
    expect(page.locator("#wage-explanation")).to_have_text("入力していません。")

    # 報酬の説明（第1-4号の元データ）
    page.locator("#wage-explanation-edit").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("報酬の説明（第1-4号）")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    expect(page.locator("#wage-explanation-applicant")).to_contain_text(worker.name_romaji)
    page.locator("#id_comparison").select_option("comparable")
    # 台帳と契約から入れる（修正 1 回目 B12）: 選んだ比較の型は残り、技能実習2号の行から 3 年の目安
    page.locator("#wage-explanation-fill").click()
    expect(page.locator("#wage-explanation-filled")).to_contain_text("技能実習2号")
    expect(page.locator("#id_comparison")).to_have_value("comparable")
    expect(page.locator("#id_applicant_duties")).to_have_value(version.duties)
    expect(page.locator("#id_applicant_experience_years")).to_have_value("3")
    expect(page.locator("#id_wage_unit")).to_have_value("monthly")
    page.locator("#id_applicant_duties").fill("製造ラインの作業員")
    page.locator("#id_applicant_experience_years").fill("2")
    page.locator("#id_japanese_duties").fill("製造ラインの作業員（入社3年目）")
    page.locator("#id_japanese_age").fill("24")
    page.locator("#id_japanese_sex").select_option("male")
    page.locator("#id_japanese_experience_years").fill("3")
    page.locator("#id_japanese_wage").fill("210000")
    page.locator("#id_has_wage_rules").select_option("true")
    page.locator("#id_rules_wage").fill("200000")
    page.locator("#id_reason").fill("賃金規程の同じ等級で決めている")
    page.get_by_role("button", name="保存する").click()
    expect(page.locator("#wage-explanation")).to_contain_text("比較対象となる日本人労働者がいる")

    # 第1-4号
    page.locator("#contract-generate").get_by_role("link", name="第1-4号を作る").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("報酬に関する説明書（第1-4号）を作る")
    expect(page.locator("#generation-findings")).to_contain_text("不足はありません")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.get_by_role("button", name="生成する").click()
    expect(page.locator("#document-status")).to_have_text("生成しました。")
    with page.expect_download() as info:
        page.locator("#document-pdf").click()
    assert info.value.suggested_filename.startswith("1-4_")
    assert page.evaluate("document.documentElement.scrollWidth") <= width

    # 契約タブへ戻り、第1-6号
    page.get_by_role("link", name=str(worker)).first.click()
    expect(page.locator("#contract-documents")).to_be_visible()
    page.locator("#contract-generate").get_by_role("link", name="第1-6号を作る").click()
    expect(page.locator("#generation-findings")).to_contain_text("不足はありません")
    page.get_by_role("button", name="生成する").click()
    expect(page.locator("#document-status")).to_have_text("生成しました。")
    assert admin_requests == []
