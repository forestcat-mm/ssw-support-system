"""通しのテスト: 操作の「…」のメニュー（修正 1 回目 B15）。

受入れ機関の詳細の支援委託契約の行と、外国人の契約タブで、主な操作 1 つのボタンと
「…」のメニューが並ぶ。
メニューはキーボード（Enter・Esc）でも開け閉めでき、外を押すと閉じる。危ない操作は赤字。
1366px でも 390px でも、画面の右にはみ出さない（メニューも画面の中に出る）。管理画面には行かない。
"""

from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, expect

from apps.core.models import User
from apps.hosts.models import SupportContract
from e2e.conftest import SignIn
from e2e.test_support_contract_filings import contract  # noqa: F401  （fixture）


def inside_viewport(page: Page, selector: str, width: int) -> None:
    box = page.locator(selector).bounding_box()
    assert box is not None
    assert box["x"] >= 0 and box["x"] + box["width"] <= width


@pytest.mark.parametrize("width", [1366, 390])
def test_action_menus(
    page: Page,
    sign_in: SignIn,
    support_staff: User,
    contract: SupportContract,  # noqa: F811
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
    page.goto(f"{base}/hosts/{contract.host_organization_id}/")
    row = page.locator(f'#support-contracts tr[data-contract="{contract.pk}"]')
    expect(row.get_by_role("link", name="編集", exact=True)).to_be_visible()
    terminate = row.get_by_role("link", name="終了を登録")
    expect(terminate).to_be_hidden()  # メニューの中
    assert page.evaluate("document.documentElement.scrollWidth") <= width

    # キーボード: summary に移って Enter で開き、Esc で閉じる（フォーカスは summary に戻る）
    summary = row.locator("details.menu > summary")
    summary.focus()
    page.keyboard.press("Enter")
    expect(terminate).to_be_visible()
    expect(terminate).to_have_class("menu__item menu__item--danger")
    inside_viewport(
        page, f'#support-contracts tr[data-contract="{contract.pk}"] .menu__list', width
    )
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.keyboard.press("Escape")
    expect(terminate).to_be_hidden()
    expect(summary).to_be_focused()

    # マウス: 開いて、外を押すと閉じる
    summary.click()
    expect(terminate).to_be_visible()
    page.locator("h1").click()
    expect(terminate).to_be_hidden()

    # 項目を押すと、その画面に移る
    summary.click()
    terminate.click()
    expect(page.locator("#id_terminated_on")).to_be_visible()

    # 外国人の契約タブ: 主な操作は「新しい版を追加」、終了の登録はメニューの赤字
    worker_id = contract.host_organization.employment_contracts.get().worker_id
    page.goto(f"{base}/workers/{worker_id}/contracts/")
    actions = page.locator("#contract-actions")
    expect(actions.get_by_role("link", name="新しい版を追加")).to_be_visible()
    actions.locator("details.menu > summary").click()
    expect(actions.get_by_role("link", name="終了を登録")).to_be_visible()
    inside_viewport(page, "#contract-actions .menu__list", width)
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    assert admin_requests == []
