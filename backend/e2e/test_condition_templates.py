"""通しのテスト: 雇用条件のひな形（T41）。

受入れ機関の詳細 →「ひな形を追加」→ 事業所・業務・時間・賃金の締切日などを入れ、基本給・契約期間に
「外国人ごとに入れる」の印を付けて保存 → 外国人の契約タブ →「雇用契約を登録」→ ひな形を選んで
「ひな形から入れる」→ 印の項目が先頭に空で出て、ほかの項目に値が入っている → 印の項目だけを入れて保存。
管理画面には行かない。
"""

from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, expect

from apps.contracts.models import EmploymentConditionTemplate, EmploymentContract
from apps.core.models import User
from apps.workers.models import ForeignWorker
from e2e.conftest import SignIn
from tests.contracts.factories import site_with_categories
from tests.hosts.factories import category
from tests.workers.factories import make_worker


@pytest.fixture
def worker() -> ForeignWorker:
    return make_worker(status="residing")


@pytest.mark.parametrize("width", [1366, 390])
def test_template_fills_the_contract_registration(
    page: Page, sign_in: SignIn, support_staff: User, worker: ForeignWorker, width: int
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
    site = site_with_categories()
    sign_in(support_staff)
    base = page.url.rstrip("/")

    # ひな形の登録
    page.goto(f"{base}/hosts/{site.host_organization_id}/")
    page.locator("#condition-template-create").click()
    page.locator("#id_name").fill("製造ライン 正社員")
    page.locator("#id_renewal").select_option("none")
    page.locator("#id_site").select_option(str(site.pk))
    page.locator(
        f'input[name="categories"][value="{category("machine_metal_processing").pk}"]'
    ).check()
    page.locator("#id_duties").fill("機械金属加工の作業")
    page.locator("#id_start_time").fill("08:30")
    page.locator("#id_end_time").fill("17:30")
    page.locator("#id_daily_working_minutes_0").fill("8")
    page.locator("#id_daily_working_minutes_1").fill("0")
    page.locator("#id_break_minutes").fill("60")
    page.locator("#id_weekly_working_minutes_0").fill("40")
    page.locator("#id_weekly_working_minutes_1").fill("0")
    page.locator("#id_weekly_working_days").fill("5")
    page.locator("#id_annual_holidays").fill("120")
    page.locator("#id_paid_leave_days").fill("10")
    page.locator("#id_wage_type").select_option("monthly")
    page.locator("#id_wage_closing_day_0").select_option("毎月末日")
    page.locator("#id_wage_payment_day_0").select_option("翌月25日")
    page.locator("#id_payment_method").select_option("bank_transfer")
    for name in ("base_wage", "starts_on", "ends_on"):
        page.locator(f'[data-field="{name}"] input[name="marked"]').check()
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.get_by_role("button", name="保存する").click()
    expect(page.get_by_text("ひな形を保存しました。")).to_be_visible()
    template = EmploymentConditionTemplate.objects.get()
    assert template.values["wage_payment_day"] == "翌月25日"
    expect(page.locator(f'#condition-templates [data-template="{template.pk}"]')).to_contain_text(
        "外国人ごとに入れる項目 3 件"
    )

    # 契約の登録でひな形を使う
    page.goto(f"{base}/workers/{worker.pk}/contracts/")
    page.get_by_role("link", name="雇用契約を登録").click()
    page.locator("#id_template").select_option(str(template.pk))
    page.get_by_role("button", name="ひな形から入れる").click()
    marked = page.locator("#template-marked")
    expect(marked.locator("#id_base_wage")).to_have_value("")
    expect(marked.locator("#id_starts_on")).to_have_value("")
    expect(page.locator("#id_duties")).to_have_value("機械金属加工の作業")
    expect(page.locator("#id_site")).to_have_value(str(site.pk))
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    marked.locator("#id_starts_on").fill("2026-11-01")
    marked.locator("#id_ends_on").fill("2027-10-31")
    marked.locator("#id_base_wage").fill("230000")
    page.get_by_role("button", name="保存する").click()
    expect(page.get_by_text("雇用契約を保存しました（第 1 版）。")).to_be_visible()
    version = EmploymentContract.objects.get(worker=worker).versions.get()
    assert (version.base_wage, version.duties) == (230000, "機械金属加工の作業")
    assert (version.wage_closing_day, version.wage_payment_day) == ("毎月末日", "翌月25日")
    assert admin_requests == []
