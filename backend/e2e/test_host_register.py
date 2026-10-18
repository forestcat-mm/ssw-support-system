"""通しのテスト（T28a）: 受入れ機関 1 社を業務画面で登録・編集する。

2 事業所・2 分野の加入・協力確認書 2 か所・支援委託契約を登録し、ダッシュボードに加入証明と
支援委託契約の期限が出る。管理画面には 1 度も行かない。
"""

from collections.abc import Iterator
from datetime import timedelta
from urllib.parse import urlparse

import pytest
from django.utils import timezone
from playwright.sync_api import Page, Request, expect

from apps.core.models import User
from apps.hosts.models import HostOrganization, HostSite
from e2e.conftest import SignIn
from tests.conftest import PDF_BYTES
from tests.hosts.factories import category, field


@pytest.fixture
def admin_requests(page: Page) -> Iterator[list[str]]:
    seen: list[str] = []

    def watch(request: Request) -> None:
        if urlparse(request.url).path.startswith("/admin/"):
            seen.append(request.url)

    page.on("request", watch)
    yield seen


def pdf(name: str) -> dict[str, object]:
    return {"name": name, "mimeType": "application/pdf", "buffer": PDF_BYTES}


def no_horizontal_scroll(page: Page, width: int) -> None:
    assert page.evaluate("document.documentElement.scrollWidth") <= width


@pytest.mark.parametrize("width", [1366, 390])
def test_register_and_edit_a_host_and_see_the_deadlines(
    page: Page, sign_in: SignIn, support_staff: User, admin_requests: list[str], width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    today = timezone.localdate()
    sign_in(support_staff)
    base = page.url.rstrip("/")
    page.goto(f"{base}/hosts/")

    # 受入れ機関
    page.get_by_role("link", name="受入れ機関を登録").click()
    page.locator("#id_name").fill("サンプル製作所株式会社")
    page.locator("#id_name_kana").fill("サンプルセイサクショ")
    page.locator("#id_corporate_number").fill("7-0000-1205-0002")
    page.locator("#id_representative_title").fill("代表取締役")
    page.locator("#id_representative_name").fill("山田 太郎")
    page.locator("#id_postal_code").fill("100-0001")
    page.locator("#id_address").fill("東京都千代田区千代田1-1")
    no_horizontal_scroll(page, width)
    page.get_by_role("button", name="保存する").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("サンプル製作所株式会社")
    host = HostOrganization.objects.get()
    assert host.corporate_number == "7000012050002"

    # 事業所（2 分野）
    for name, address, code, skill_field, category_code in (
        (
            "本社工場",
            "東京都千代田区千代田1-1",
            "2194",
            "industrial_products",
            "machine_metal_processing",
        ),
        (
            "食品工場",
            "埼玉県サンプル市本町2-2",
            "0999",
            "food_manufacturing",
            "food_manufacturing_general",
        ),
    ):
        page.locator("#sites").get_by_role("link", name="追加").click()
        page.locator("#id_name").fill(name)
        page.locator("#id_postal_code").fill("100-0001")
        page.locator("#id_address").fill(address)
        page.locator("#id_industry_code").fill(code)
        page.locator("#id_field").select_option(str(field(skill_field).pk))
        page.locator(f'input[name="categories"][value="{category(category_code).pk}"]').check()
        no_horizontal_scroll(page, width)
        page.get_by_role("button", name="保存する").click()
        expect(page.locator("#sites")).to_contain_text(name)
    head = HostSite.objects.get(name="本社工場")
    food_site = HostSite.objects.get(name="食品工場")
    expect(page.locator('#cooperation-status [data-status="not-submitted"]')).to_have_count(2)

    # 加入状況（2 分野）。工業は期限が 20 日後（1 か月前から起票）、飲食は期限なし
    for skill_field, valid_until in (
        ("industrial_products", (today + timedelta(days=20)).isoformat()),
        ("food_manufacturing", ""),
    ):
        page.locator("#memberships").get_by_role("link", name="追加").click()
        page.locator("#id_field").select_option(str(field(skill_field).pk))
        page.locator("#id_joined_on").fill("2025-04-01")
        page.locator("#id_valid_until").fill(valid_until)
        page.locator("#id_certificate").set_input_files(pdf(f"{skill_field}.pdf"))
        page.get_by_role("button", name="保存する").click()
        expect(page.get_by_text("保存しました。")).to_be_visible()
    expect(page.get_by_text("ダッシュボードに期限のタスクを 1 件起票しました。")).to_have_count(0)
    memberships = page.locator("#memberships")
    expect(memberships).to_contain_text("industrial_products.pdf")
    expect(memberships).to_contain_text("期限なし")

    # 協力確認書（2 か所: 本社工場の所在地の区と、食品工場の所在地の市）
    for site, municipality in ((head, "千代田区"), (food_site, "サンプル市")):
        page.locator("#cooperation").get_by_role("link", name="提出を登録").click()
        page.locator("#id_site").select_option(str(site.pk))
        page.locator("#id_municipality").fill(municipality)
        page.locator("#id_submitted_on").fill("2025-04-10")
        page.locator("#id_uploads").set_input_files(pdf(f"控え_{municipality}.pdf"))
        page.get_by_role("button", name="保存する").click()
        expect(page.get_by_text("保存しました。")).to_be_visible()
    status = page.locator("#cooperation-status")
    expect(status.locator('[data-status="not-submitted"]')).to_have_count(0)
    expect(status.locator(f'tr[data-site="{head.pk}"]')).to_contain_text("千代田区 2025-04-10")
    expect(status.locator(f'tr[data-site="{food_site.pk}"]')).to_contain_text("サンプル市")

    # 支援委託契約（終了が 60 日後。3 か月前から起票）と契約書
    page.locator("#support-contracts").get_by_role("link", name="追加").click()
    page.locator("#id_concluded_on").fill("2025-03-15")
    page.locator("#id_starts_on").fill("2025-04-01")
    page.locator("#id_ends_on").fill((today + timedelta(days=60)).isoformat())
    page.locator("#id_uploads").set_input_files(pdf("支援委託契約書.pdf"))
    page.get_by_role("button", name="保存する").click()
    expect(page.get_by_text("ダッシュボードに期限のタスクを 1 件起票しました。")).to_be_visible()
    expect(page.locator("#support-contracts")).to_contain_text("支援委託契約書.pdf")
    no_horizontal_scroll(page, width)

    # 編集: 基本情報の電話番号と、事業所の所在地（協力確認書の出し直しを知らせる）
    page.locator("#host-basic").get_by_role("link", name="編集").click()
    page.locator("#id_phone_number").fill("03-0000-0000")
    page.get_by_role("button", name="保存する").click()
    expect(page.locator("#host-basic")).to_contain_text("03-0000-0000")
    page.locator(f'#sites tr[data-site="{food_site.pk}"]').get_by_role("link", name="編集").click()
    page.locator("#id_address").fill("埼玉県サンプル市新町3-3")
    page.get_by_role("button", name="保存する").click()
    expect(page.get_by_text("協力確認書の記載事項が変わったので")).to_be_visible()

    # ダッシュボード: 加入証明（20 日後）と支援委託契約（60 日後）の期限
    page.goto(f"{base}/")
    rows = page.locator("#dashboard-filings tbody tr")
    expect(rows).to_have_count(2)
    expect(rows.filter(has_text="加入証明の更新")).to_contain_text(
        (today + timedelta(days=20)).strftime("%Y-%m-%d")
    )
    expect(rows.filter(has_text="支援委託契約の更新")).to_contain_text(
        (today + timedelta(days=60)).strftime("%Y-%m-%d")
    )
    no_horizontal_scroll(page, width)
    assert admin_requests == []
