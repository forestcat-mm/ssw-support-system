"""通しのテスト: 入力の補助（修正 1 回目 B13・B14・B17）。

受入れ機関の登録の画面で、必須の欄のラベルが太字、郵便番号・電話番号・法人番号は欄を離れると直して知らせ、
保存していない変更があるときは、画面の中のリンクで確かめのダイアログ、移動でブラウザの確かめが出る。
保存のボタンでの送信では出ない。管理画面には 1 度も行かない。
"""

from urllib.parse import urlparse

import pytest
from playwright.sync_api import Dialog, Page, expect

from apps.core.models import User
from apps.hosts.models import HostOrganization
from e2e.conftest import SignIn


@pytest.mark.parametrize("width", [1366, 390])
def test_normalize_bold_labels_and_unsaved_changes(
    page: Page, sign_in: SignIn, support_staff: User, width: int
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
    dialogs: list[str] = []

    def seen(dialog: Dialog) -> None:
        dialogs.append(dialog.type)  # 受け流しは conftest の leave_unsaved_pages

    page.on("dialog", seen)
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(support_staff)
    base = page.url.rstrip("/")
    page.goto(f"{base}/hosts/new/")

    # B14: 必須の欄のラベルは太字、任意の欄は太字でない
    name_label = page.locator('label[for="id_name"]')
    expect(name_label).to_have_class("form-row__label form-row__label--required")
    assert name_label.evaluate("label => getComputedStyle(label).fontWeight") == "700"
    phone_label = page.locator('label[for="id_phone_number"]')
    assert phone_label.evaluate("label => getComputedStyle(label).fontWeight") != "700"

    # B13: 欄を離れると直して知らせる
    page.locator("#id_postal_code").fill("１０００００１")
    page.locator("#id_postal_code").press("Tab")
    expect(page.locator("#id_postal_code")).to_have_value("100-0001")
    expect(page.locator("#id_postal_code-note")).to_contain_text("「100-0001」に直しました。")
    page.locator("#id_phone_number").fill("０３－０００ー0000")
    page.locator("#id_phone_number").press("Tab")
    expect(page.locator("#id_phone_number")).to_have_value("03-000-0000")
    page.locator("#id_corporate_number").fill("1234567890123")
    page.locator("#id_corporate_number").press("Tab")
    expect(page.locator("#id_corporate_number-note")).to_contain_text("検査用数字")
    page.locator("#id_corporate_number").fill("７０００－０１２０－５０００２")
    page.locator("#id_corporate_number").press("Tab")
    expect(page.locator("#id_corporate_number")).to_have_value("7000012050002")
    expect(page.locator("#id_corporate_number-note")).not_to_contain_text("検査用数字")
    assert page.evaluate("document.documentElement.scrollWidth") <= width

    # B17: 画面の中のリンクで移るときは確かめる。「このページに残る」なら入力は残る
    page.locator("#id_name").click()
    page.keyboard.type("サンプル製作所株式会社")
    page.locator(".page-header").get_by_role("link").first.click()
    unsaved = page.locator("#unsaved-dialog")
    expect(unsaved).to_be_visible()
    unsaved.get_by_role("button", name="このページに残る").click()
    expect(unsaved).to_be_hidden()
    assert urlparse(page.url).path == "/hosts/new/"
    expect(page.locator("#id_name")).to_have_value("サンプル製作所株式会社")
    page.locator(".page-header").get_by_role("link").first.click()
    unsaved.get_by_role("button", name="保存せずに移動").click()
    page.wait_for_url(f"{base}/hosts/")
    assert dialogs == []  # 画面の中のダイアログで確かめたので、ブラウザの確かめは出ない

    # 移動（アドレスの入力・戻るなど）ではブラウザの確かめが出る
    page.goto(f"{base}/hosts/new/")
    page.locator("#id_name").click()
    page.keyboard.type("サンプル製作所株式会社")
    page.goto(f"{base}/hosts/")
    assert dialogs == ["beforeunload"]

    # 保存のボタンでの送信では出ない
    page.goto(f"{base}/hosts/new/")
    page.locator("#id_name").click()
    page.keyboard.type("サンプル製作所株式会社")
    page.locator("#id_representative_name").fill("山田 太郎")
    page.locator("#id_postal_code").fill("1000001")
    page.locator("#id_address").fill("東京都千代田区千代田1-1")
    page.locator("#id_corporate_number").fill("7000012050002")
    page.get_by_role("button", name="保存する").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("サンプル製作所株式会社")
    assert dialogs == ["beforeunload"]
    assert HostOrganization.objects.get().postal_code == "100-0001"  # サーバーでも直す
    assert admin_requests == []
