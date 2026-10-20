"""通しのテスト: 第1-26号 公的義務履行に関する誓約書（T42 の追加）。

申請の書類の一覧（変更）の第1-26号の行 →「生成する」→ 理由（やむを得ない理由の文）を入れて
確かめ直す → 生成 → 生成物の画面。管理画面には行かない。
"""

from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, expect

from apps.contracts.models import EmploymentContract
from apps.core.models import User
from apps.forms_engine.models import GeneratedDocument
from e2e.conftest import SignIn
from e2e.test_application_documents import contract  # noqa: F401  fixture


@pytest.mark.parametrize("width", [1366, 390])
def test_generate_the_pledge_with_a_reason(
    page: Page,
    sign_in: SignIn,
    support_staff: User,
    contract: EmploymentContract,  # noqa: F811
    width: int,
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
    page.goto(
        f"{base}/workers/{contract.worker_id}/documents/application/?set_id=chg&basis_on=2026-10-20"
    )
    row = page.locator('#application-set-rows [data-row="chg_t1-17-1"]')
    expect(row).to_contain_text("該当するかを確かめてください")
    page.goto(f"{base}/workers/{contract.worker_id}/documents/public-duty-pledge/")
    page.get_by_label("ほかのやむを得ない理由").check()
    page.locator("#id_other_reason").fill("勤務先の倒産で収入が無かったため")
    page.get_by_role("button", name="検査する").click()
    expect(page.locator("#generation-findings")).to_contain_text("不足はありません")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.get_by_role("button", name="生成する").click()
    expect(page.locator("#document-status")).to_have_text("生成しました。")
    assert GeneratedDocument.objects.get(template__form_id="1-26").worker_id == contract.worker_id
    assert admin_requests == []
