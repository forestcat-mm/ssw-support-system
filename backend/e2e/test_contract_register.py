"""通しのテスト（T28b）: 雇用契約を業務画面で登録し、報酬を変えた新しい版を足し、終了を登録する。

変更・終了の届出タスクが起票されることを確かめる。管理者は終了を理由つきで取り消せる。
管理画面には 1 度も行かない。
"""

from collections.abc import Iterator
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Request, expect

from apps.contracts.models import EmploymentContract
from apps.core.models import User
from apps.filings.models import Filing, FilingKind, FilingStatus
from apps.workers.models import ForeignWorker
from e2e.conftest import SignIn
from tests.contracts.factories import site_with_categories
from tests.hosts.factories import category
from tests.workers.factories import make_worker


@pytest.fixture
def admin_requests(page: Page) -> Iterator[list[str]]:
    seen: list[str] = []

    def watch(request: Request) -> None:
        if urlparse(request.url).path.startswith("/admin/"):
            seen.append(request.url)

    page.on("request", watch)
    yield seen


@pytest.fixture
def worker() -> ForeignWorker:
    return make_worker(status="residing")


def no_horizontal_scroll(page: Page, width: int) -> None:
    assert page.evaluate("document.documentElement.scrollWidth") <= width


@pytest.mark.parametrize("width", [1366, 390])
def test_register_change_and_terminate_a_contract(
    page: Page,
    sign_in: SignIn,
    support_staff: User,
    administrator: User,
    worker: ForeignWorker,
    admin_requests: list[str],
    width: int,
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    site = site_with_categories()  # テスト工業株式会社 本社工場（機械金属加工・縫製）
    sign_in(support_staff)
    base = page.url.rstrip("/")
    page.goto(f"{base}/workers/{worker.pk}/contracts/")

    # 契約の登録（第1版）
    page.get_by_role("link", name="雇用契約を登録").click()
    page.locator("#id_starts_on").fill("2026-04-01")
    page.locator("#id_ends_on").fill("2027-03-31")
    page.locator("#id_renewal").select_option("none")
    page.locator("#id_site").select_option(str(site.pk))
    page.locator(
        f'input[name="categories"][value="{category("machine_metal_processing").pk}"]'
    ).check()
    page.locator("#id_duties").fill("機械金属加工の作業")
    page.locator("#id_start_time").fill("08:30")
    page.locator("#id_end_time").fill("17:30")
    page.locator("#id_daily_working_minutes").fill("480")
    page.locator("#id_break_minutes").fill("60")
    page.locator("#id_weekly_working_minutes").fill("2400")
    page.locator("#id_weekly_working_days").fill("5")
    page.locator("#id_annual_holidays").fill("120")
    page.locator("#id_paid_leave_days").fill("10")
    page.locator("#id_wage_type").select_option("monthly")
    page.locator("#id_base_wage").fill("220000")
    page.locator("#id_wage_closing_day").fill("毎月末日")
    page.locator("#id_wage_payment_day").fill("翌月25日")
    page.locator("#id_payment_method").select_option("bank_transfer")
    page.get_by_role("button", name="手当を追加").click()
    page.locator("#id_allowances-0-name").fill("住宅手当")
    page.locator("#id_allowances-0-amount").fill("10000")
    no_horizontal_scroll(page, width)
    page.get_by_role("button", name="保存する").click()
    expect(page.get_by_text("雇用契約を保存しました（第 1 版）。")).to_be_visible()
    contract = EmploymentContract.objects.get(worker=worker)
    assert contract.host_organization == site.host_organization

    # 新しい版（基本給の減額 → 変更届出）。前の版の値と手当が写っている
    page.get_by_role("link", name="新しい版を追加").click()
    expect(page.locator("#id_base_wage")).to_have_value("220000")
    expect(page.locator("#id_allowances-0-name")).to_have_value("住宅手当")
    expect(page.locator("#id_changed_on")).to_have_value("")
    page.locator("#id_changed_on").fill("2026-10-01")
    page.locator("#id_change_reason").fill("基本給の改定")
    page.locator("#id_base_wage").fill("210000")
    page.get_by_role("button", name="保存する").click()
    expect(page.get_by_text("雇用契約を保存しました（第 2 版）。")).to_be_visible()
    expect(page.get_by_text("届出タスクを起票しました: 雇用契約変更届出。")).to_be_visible()
    expect(page.locator("#diff")).to_contain_text("210,000")
    no_horizontal_scroll(page, width)

    # 終了の登録（会社都合 → 終了届出と受入れ困難届出）
    page.get_by_role("link", name="終了を登録").click()
    page.locator("#id_terminated_on").fill("2026-10-15")
    page.locator("#id_termination_reason").select_option("company")
    page.get_by_role("button", name="終了を登録").click()
    expect(page.get_by_text("終了を登録しました。届出タスクを起票しました。")).to_be_visible()
    kinds = set(Filing.objects.filter(worker=worker).values_list("kind", flat=True))
    assert {
        FilingKind.CONTRACT_CHANGE,
        FilingKind.CONTRACT_TERMINATION,
        FilingKind.ACCEPTANCE_DIFFICULTY,
    } <= kinds
    expect(page.get_by_role("link", name="終了を取り消す")).to_have_count(0)  # 管理者だけ

    # 管理者が理由つきで終了を取り消す
    page.context.clear_cookies()
    sign_in(administrator)
    page.goto(f"{base}/workers/{worker.pk}/contracts/?contract={contract.pk}")
    page.get_by_role("link", name="終了を取り消す").click()
    page.locator("#id_reason").fill("別の契約に登録した")
    page.get_by_role("button", name="終了を取り消す").click()
    expect(page.get_by_text("終了を取り消しました。")).to_be_visible()
    task = Filing.objects.get(worker=worker, kind=FilingKind.CONTRACT_TERMINATION)
    assert task.status == FilingStatus.CANCELLED
    no_horizontal_scroll(page, width)
    assert admin_requests == []
