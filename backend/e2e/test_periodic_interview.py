"""通しのテスト: ダッシュボードの「定期面談」→ 記録 → タスクが完了し、次回のタスクが出る（T19）。"""

from datetime import date

import pytest
from playwright.sync_api import Locator, Page, expect

from apps.core.models import User
from apps.filings.models import Filing, FilingKind, FilingStatus, Trigger
from apps.workers.models import WorkerStatus
from e2e.conftest import SignIn, answer_all_no
from services import deadlines, filings, form_templates
from tests.contracts.factories import make_contract, site_with_categories
from tests.core.factories import make_staff
from tests.workers.factories import make_worker


@pytest.fixture
def interview_task() -> Filing:
    form_templates.load_all()
    worker = make_worker(
        assigned_staff=make_staff(name="佐藤 花子", position="支援担当"),
        status=WorkerStatus.RESIDING,
    )
    make_contract(worker, site=site_with_categories())
    filing, _created = filings.create(
        FilingKind.PERIODIC_INTERVIEW,
        target=worker,
        basis_on=date(2026, 4, 1),
        trigger=Trigger.INTERVIEW_CYCLE,
    )
    return filing


@pytest.mark.parametrize("width", [1366, 390])
def test_record_from_dashboard_completes_the_task(
    page: Page, sign_in: SignIn, support_staff: User, interview_task: Filing, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(support_staff)
    row = page.locator(f'#dashboard-filings tr[data-filing="{interview_task.pk}"]')
    expect(row).to_contain_text("定期面談（3か月ごと）")
    row.get_by_role("link", name="記録", exact=True).click()

    expect(page.get_by_role("heading", level=1)).to_have_text("定期面談の記録")
    expect(page.locator("#interview-task")).to_be_visible()
    expect(page.locator("#interview-self")).not_to_contain_text("電話")  # 音声だけの電話は不可
    page.locator("#id_self-body").fill("仕事・生活とも問題なし。")
    page.locator("#id_supervisor-counterpart_name").fill("工場長 田中")
    page.locator("#id_supervisor-body").fill("勤務態度は良好。")
    # 報告書の質問（T35）: すべて「問題無」にしてから、1 問だけ「問題有」にして内容を書く
    answer_all_no(page)
    page.locator('input[name="self_answers-answer_2_4"][value="yes"]').check()
    page.locator("#id_self_answers-detail_2_4").fill("寮の窓が壊れている。")
    page.get_by_role("button", name="保存する").click()

    expect(page.get_by_text("定期面談を保存し、タスクを完了にしました。")).to_be_visible()
    expect(page.locator("#periodic-interview")).to_contain_text("完了")
    expect(page.locator('[data-counterpart="supervisor"]')).to_contain_text("工場長 田中")
    expect(page.locator('[data-answers="worker"]')).to_contain_text("寮の窓が壊れている。")
    assert page.evaluate("document.documentElement.scrollWidth") <= width

    # 報告書（第5-5号・第5-6号）をまとめて生成し、帳票タブに 2 つ出る（T35-D12）
    page.locator("#interview-generate").click()
    expect(page.locator("#generation-findings")).to_contain_text("不足はありません")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.get_by_role("button", name="生成する").click()
    expect(
        page.get_by_text("定期面談報告書（第5-5号・第5-6号）の生成を依頼しました。")
    ).to_be_visible()
    expect(page.locator("main")).to_contain_text("5-5")
    expect(page.locator("main")).to_contain_text("5-6")
    assert page.evaluate("document.documentElement.scrollWidth") <= width

    # ダッシュボードには完了したタスクが消え、次回のタスク（今日から 3 か月）が出る
    interview_task.refresh_from_db()
    assert interview_task.status == FilingStatus.COMPLETED
    following = Filing.objects.get(
        kind=FilingKind.PERIODIC_INTERVIEW, status=FilingStatus.NOT_STARTED
    )
    assert following.due_on == deadlines.periodic_interview(following.basis_on).due_on
    page.goto(page.url.split("/workers/")[0] + "/")
    expect(page.locator(f'#dashboard-filings tr[data-filing="{interview_task.pk}"]')).to_have_count(
        0
    )
    expect(page.locator(f'#dashboard-filings tr[data-filing="{following.pk}"]')).to_be_visible()


REPORT_FIELDS = (
    "#id_interview-report_to",
    "#id_interview-report_to_note",
    "#id_interview-reported_on",
)


@pytest.mark.parametrize("width", [1366, 390])
def test_report_fields_follow_the_violation_and_the_need(
    page: Page, sign_in: SignIn, support_staff: User, interview_task: Filing, width: int
) -> None:
    """違反の疑いが「なし」なら通報の欄はすべて無効で空、要否が「不要」なら通報先・通報日が無効で空。"""
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(support_staff)
    assert interview_task.worker_id is not None
    page.goto(f"{page.url.rstrip('/')}/workers/{interview_task.worker_id}/support/interviews/new/")

    def violation(value: str) -> Locator:
        return page.locator(f'input[name="interview-violation_suspected"][value="{value}"]')

    def needed(value: str) -> Locator:
        return page.locator(f'input[name="interview-report_required"][value="{value}"]')

    # 開いたとき（「なし」）は、要否も通報先・通報日も入力できない
    expect(needed("yes")).to_be_disabled()
    for selector in REPORT_FIELDS:
        expect(page.locator(selector)).to_be_disabled()

    violation("yes").check()
    expect(needed("yes")).to_be_enabled()
    expect(page.locator("#id_interview-report_to")).to_be_disabled()  # 要否を選ぶまでは無効
    needed("yes").check()
    page.locator("#id_interview-report_to").select_option("other")
    page.locator("#id_interview-report_to_note").fill("市役所")
    page.locator("#id_interview-reported_on").fill("2026-10-01")

    # 「不要」にすると通報先・通報日が無効になり、値が空になる
    needed("no").check()
    for selector in REPORT_FIELDS:
        expect(page.locator(selector)).to_be_disabled()
        expect(page.locator(selector)).to_have_value("")
    # 「要」に戻すと入力できる（値は空のまま）
    needed("yes").check()
    for selector in REPORT_FIELDS:
        expect(page.locator(selector)).to_be_enabled()
    page.locator("#id_interview-report_to").select_option("labor_standards_office")

    # 違反の疑いを「なし」にすると、要否の選択も外れ、すべて無効で空になる
    violation("no").check()
    expect(needed("yes")).to_be_disabled()
    expect(needed("yes")).not_to_be_checked()
    for selector in REPORT_FIELDS:
        expect(page.locator(selector)).to_be_disabled()
        expect(page.locator(selector)).to_have_value("")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
