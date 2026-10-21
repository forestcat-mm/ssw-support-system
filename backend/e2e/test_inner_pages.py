"""通しのテスト: 外国人の詳細の形を広げた画面（U7）。

受入れ機関の詳細（上の帯・今やること・番号つきのセクション・目次・スクロールしたときの細い帯）、
一括申請の中の画面と設定の画面（上の帯の名前と状態・番号つきのセクション）。
1366px と 390px で横にはみ出さず、管理画面へ行かない。
"""

from datetime import date, timedelta
from urllib.parse import urlparse

import pytest
from django.utils import timezone
from playwright.sync_api import Page, Request, expect

from apps.core.models import User
from apps.filings.models import FilingKind, Trigger
from apps.forms_engine.models import BulkApplication
from apps.hosts.models import HostMembership, HostOrganization, SupportContract
from e2e.conftest import SignIn
from services import filings, form_templates, host_records
from tests.forms_engine.bulk_cases import complete_application
from tests.hosts.factories import make_host, make_site


def watch_admin(page: Page) -> list[str]:
    seen: list[str] = []

    def watch(request: Request) -> None:
        if urlparse(request.url).path.startswith("/admin/"):
            seen.append(request.url)

    page.on("request", watch)
    return seen


@pytest.fixture
def host() -> HostOrganization:
    host = make_host(name="（ダミー）サンプル精機工業株式会社")
    site = make_site(host)
    HostMembership.objects.create(
        host_organization=host,
        field=site.field,
        joined_on=date(2024, 8, 12),
        member_number="DUMMY-0001",
    )
    today = timezone.localdate()
    contract = SupportContract(
        host_organization=host,
        concluded_on=today - timedelta(days=335),
        starts_on=today - timedelta(days=335),
        ends_on=today + timedelta(days=30),
        monthly_fee=30000,
    )
    host_records.save_support_contract(contract, by=None, today=today)
    filings.create(
        FilingKind.SUPPORT_CONTRACT_RENEWAL,
        target=contract,
        basis_on=today + timedelta(days=30),
        trigger=Trigger.SUPPORT_CONTRACT_END,
    )
    return host


@pytest.mark.parametrize("width", [1366, 390])
def test_host_detail_band_todo_and_contents(
    page: Page, sign_in: SignIn, administrator: User, host: HostOrganization, width: int
) -> None:
    admin_requests = watch_admin(page)
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(administrator)
    base = page.url.rstrip("/")
    page.goto(f"{base}/hosts/{host.pk}/")
    head = page.locator("#person-head")
    expect(head.get_by_role("heading", level=1)).to_have_text(host.name)
    expect(head.locator(".person-photo__initial")).to_have_text("サ")
    expect(page.locator("#support-contract-expiry")).to_be_visible()
    expect(page.locator("#todo [data-todo]").first).to_contain_text("支援委託契約の更新")
    toc = page.get_by_role("navigation", name="このページの目次")
    expect(toc.get_by_role("link").first).to_have_text("1 基本情報")
    toc.get_by_role("link", name="5 支援委託契約").click()
    bar = page.locator("#person-bar")
    expect(bar).to_be_visible()
    expect(bar.locator('[aria-current="location"]')).to_have_text("5 支援委託契約")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    if width == 1366:
        # 表は枠に収まる（右端の列が枠で切れない。U4〜U8 の見比べの直し）
        cut = page.evaluate(
            """() => [...document.querySelectorAll('.doc .table-scroll')]
                .filter((wrap) => wrap.scrollWidth > wrap.clientWidth + 1)
                .map((wrap) => wrap.closest('section').id)"""
        )
        assert cut == []
    assert admin_requests == []


@pytest.mark.parametrize("width", [1366, 390])
def test_bulk_detail_and_settings_have_the_band_and_numbers(
    page: Page, sign_in: SignIn, administrator: User, width: int
) -> None:
    form_templates.load_all()
    application: BulkApplication = complete_application("extension")
    admin_requests = watch_admin(page)
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(administrator)
    base = page.url.rstrip("/")
    page.goto(f"{base}/forms/bulk/{application.pk}/")
    band = page.locator("#page-band")
    expect(band.locator("#bulk-state")).to_have_text("Excel をまだ作っていない")
    # 番号は CSS の counter（.numbered の直下のセクションの見出しの前）
    expect(page.locator(".numbered > section#rows > .card__header > .card__title")).to_have_count(1)
    assert page.evaluate("document.documentElement.scrollWidth") <= width

    page.goto(f"{base}/settings/")
    expect(page.locator("#page-band h1")).to_have_text("設定")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    assert admin_requests == []
