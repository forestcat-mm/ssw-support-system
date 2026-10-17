"""段階2の受け入れ確認（T27 の A。make gate2 のときだけ動く。通常の make e2e では飛ばす）。

seed_dummy を流し、担当者 2 人（dummy_tanto・dummy_tanto2）が、定期面談の対象
（在留中で雇用契約のある人）の全員の定期面談をダッシュボードの「記録」から画面で記録し、
相談 5 件（うち 2 件は経過の追記で解決）を記録する。管理画面（/admin/）には 1 度も行かない。

- 基準日は今日。GATE_DATE=YYYY-MM-DD で変えられる（django.utils.timezone.now をずらす。
  live_server は同じプロセスで動くので、画面の「今日」も基準日になる）。
- 期待値（期限の日付・警告レベル）は日付を直接書かず、基準日と seed の場面の定数から
  services.deadlines の関数で計算する。
- 操作の時間は参考として出すだけ（合否には使わない。手間の測定は人が行う。docs/tasks-phase2.md）。
"""

import os
import re
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
from urllib.parse import urlparse

import pytest
from django.core.management import call_command
from django.db import models
from django.utils import timezone
from playwright.sync_api import Page, Request, expect
from pytest_django.fixtures import Settings
from pytest_django.live_server_helper import LiveServer

from apps.contracts.models import EmploymentContract, EmploymentContractVersion
from apps.core.models import Staff, User
from apps.filings.models import Filing, FilingKind, FilingStatus
from apps.seed import support_builders
from apps.seed.context import DUMMY_USERNAME, DUMMY_USERNAME_2
from apps.seed.models import SeedObject
from apps.support.models import (
    Consultation,
    ConsultationStatus,
    Counterpart,
    PeriodicInterview,
    SupportRecord,
)
from apps.workers.models import ForeignWorker, WorkerStatus
from e2e.conftest import SignIn
from services import dashboard, deadlines, filings, periodic_report
from services.support_catalog import SupportItem
from tests.core.factories import TEST_PASSWORD

pytestmark = pytest.mark.skipif(os.environ.get("GATE2") != "1", reason="make gate2 のときだけ動く")

OPEN = list(filings.OPEN_STATUSES)
USERS = (DUMMY_USERNAME, DUMMY_USERNAME_2)
CONSULTATIONS_PER_USER = {
    DUMMY_USERNAME: 3,
    DUMMY_USERNAME_2: 2,
}  # 各担当者の 1 件目を解決まで進める


# --- 基準日と seed --------------------------------------------------------------------------


@pytest.fixture
def base_date(monkeypatch: pytest.MonkeyPatch) -> date:
    """基準日（GATE_DATE、無ければ今日）。指定があればアプリの「今」を同じ日数だけずらす。"""
    raw = os.environ.get("GATE_DATE", "")
    if raw:
        offset = date.fromisoformat(raw) - timezone.localdate()
        real_now = timezone.now

        def shifted() -> datetime:
            return real_now() + offset

        monkeypatch.setattr(timezone, "now", shifted)
    return timezone.localdate()


@pytest.fixture
def seeded(settings: Settings, tmp_path: Path, base_date: date) -> list[ForeignWorker]:
    """seed_dummy の 30 人（外国人の番号の順）。ログイン用の 2 人にパスワードを設定する。"""
    settings.DEBUG = True
    settings.ALLOW_DUMMY_DATA = True
    settings.STORAGES = {
        **settings.STORAGES,
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path / "media")},
        },
    }
    call_command("seed_dummy", "--date", base_date.isoformat(), stdout=StringIO())
    ids = SeedObject.objects.filter(content_type__model="foreignworker").values("object_id")
    for username in USERS:
        user = User.objects.get(username=username)
        user.set_password(TEST_PASSWORD)
        user.save(update_fields=["password"])
    return list(ForeignWorker.objects.filter(pk__in=ids).order_by("pk"))


def interview_targets(today: date) -> set[int]:
    """定期面談の対象（在留中・無効化していない・今日を期間に含む雇用契約がある人）。"""
    first_starts = EmploymentContractVersion.objects.filter(contract=models.OuterRef("pk"))
    contracts = (
        EmploymentContract.objects.filter(
            worker__status=WorkerStatus.RESIDING,
            worker__deactivated_at__isnull=True,
            terminated_on__isnull=True,
        )
        .filter(models.Q(current_ends_on__isnull=True) | models.Q(current_ends_on__gte=today))
        .annotate(
            first_starts_on=models.Subquery(
                first_starts.order_by("starts_on").values("starts_on")[:1]
            )
        )
        .filter(first_starts_on__lte=today)
    )
    return set(contracts.values_list("worker_id", flat=True))


def expected_basis(number: int, worker: ForeignWorker, base: date) -> date:
    """seed の場面（support_builders の定数）から、開いている定期面談のタスクの起算日を出す。"""
    if number == support_builders.REPORTED_INTERVIEW:
        return base + timedelta(days=support_builders.REPORT_DAY)
    # 8 番の D−3 の組は取り消し済み（タスクは前の起算日に戻る）
    last = base - timedelta(days=support_builders.last_interview_days(number))
    first_start = (
        EmploymentContractVersion.objects.filter(contract__worker=worker)
        .order_by("starts_on")
        .values_list("starts_on", flat=True)
        .first()
    )
    assert first_start is not None
    return last if last > first_start else first_start


# --- 画面の操作 ------------------------------------------------------------------------------


@dataclass
class Timing:
    username: str
    interviews: list[float]
    consultations: list[float]


@pytest.fixture
def admin_requests(page: Page) -> Iterator[list[str]]:
    """管理画面（/admin/）へのリクエスト。最後に 0 件であることを確かめる。"""
    seen: list[str] = []

    def watch(request: Request) -> None:
        if urlparse(request.url).path.startswith("/admin/"):
            seen.append(request.url)

    page.on("request", watch)
    yield seen


def check_dashboard(
    page: Page, tasks: list[Filing], expected: dict[int, deadlines.Deadline]
) -> None:
    """自分の担当の定期面談のタスクが、基準日から計算した期限の日付・警告レベルで出ている。"""
    today = timezone.localdate()
    expect(page.locator("#dashboard-filings tbody tr")).to_have_count(len(tasks))
    levels: Counter[str] = Counter()
    for task in tasks:
        deadline = expected[task.pk]
        level = deadlines.assess(deadline, today).level
        levels[level] += 1
        row = page.locator(f'#dashboard-filings tr[data-filing="{task.pk}"]')
        expect(row.locator("[data-level]")).to_have_attribute("data-level", str(level))
        if level is not deadlines.Level.NOT_YET:  # 受付前はバッジが日付（日付の欄を重ねない）
            expect(row.locator(".mono").first).to_have_text(deadline.unadjusted_due_on.isoformat())
    for level in dashboard.COUNTED_LEVELS:
        expect(page.locator(f'[data-level-count="{level}"]')).to_have_text(
            re.compile(rf"(?<!\d){levels[level]}件")
        )


def record_interviews(
    page: Page, base_url: str, tasks: list[Filing], expected: dict[int, deadlines.Deadline]
) -> list[float]:
    """ダッシュボード（既定は自分の担当）の定期面談の各行の「記録」から記録する。"""
    listing = f"{base_url}/?kind={FilingKind.PERIODIC_INTERVIEW}"
    today = timezone.localdate()
    page.goto(listing)
    check_dashboard(page, tasks, expected)
    seconds = []
    for task in tasks:
        page.goto(listing)
        started = time.monotonic()
        row = page.locator(f'#dashboard-filings tr[data-filing="{task.pk}"]')
        row.get_by_role("link", name="記録", exact=True).click()
        expect(page.get_by_role("heading", level=1)).to_have_text("定期面談の記録")
        expect(page.locator("#id_self-occurred_on")).to_have_value(today.isoformat())
        page.locator("#id_self-body").fill("仕事・生活とも問題なし。（受け入れ確認）")
        page.locator("#id_supervisor-counterpart_name").fill("工場長 田中")
        page.locator("#id_supervisor-body").fill("勤務態度・労働時間とも問題なし。（受け入れ確認）")
        page.get_by_role("button", name="保存する").click()
        expect(page.get_by_text("定期面談を保存し、タスクを完了にしました。")).to_be_visible()
        seconds.append(time.monotonic() - started)

    # 記録したタスクは一覧から消え、次回のタスク（基準日が起算日）が 1 人 1 件ずつ出る
    workers = Counter(task.worker_id for task in tasks)
    following = list(
        Filing.objects.filter(
            kind=FilingKind.PERIODIC_INTERVIEW, status__in=OPEN, worker_id__in=list(workers)
        ).order_by("pk")
    )
    assert Counter(task.worker_id for task in following) == workers
    for task in following:
        assert task.basis_on == today
    next_deadline = deadlines.periodic_interview(today)
    page.goto(listing)
    check_dashboard(page, following, {task.pk: next_deadline for task in following})
    return seconds


def record_consultations(page: Page, base_url: str, workers: list[ForeignWorker]) -> list[float]:
    """相談を受け付ける。1 件目は経過の追記で解決にする。"""
    seconds = []
    for index, worker in enumerate(workers):
        started = time.monotonic()
        page.goto(f"{base_url}/workers/{worker.pk}/support/records/")
        page.locator("#record-kinds").get_by_role("link", name="相談・苦情への対応").click()
        expect(page.get_by_role("heading", level=1)).to_have_text("相談の受付")
        page.get_by_label("主な分類").select_option("daily_life")
        page.locator("#id_body").fill("住まいの近くの病院を知りたいという相談。（受け入れ確認）")
        page.get_by_role("button", name="保存する").click()
        expect(page.get_by_text("相談を受け付けました。")).to_be_visible()
        if index == 0:
            page.get_by_role("link", name="経過を追記").click()
            page.get_by_label("追記後の対応状況").select_option(str(ConsultationStatus.RESOLVED))
            page.locator("#id_body").fill("病院の一覧を渡し、本人も了承した。（受け入れ確認）")
            page.get_by_role("button", name="追加", exact=True).click()
            expect(page.get_by_text("経過を追記しました。")).to_be_visible()
            expect(page.locator("[data-consultation-status]")).to_have_text("解決")
        seconds.append(time.monotonic() - started)
    return seconds


def sign_out(page: Page) -> None:
    page.get_by_role("button", name="ログアウト").click()
    expect(page.get_by_label("ユーザー名")).to_be_visible()


def special_event_keys(fiscal_year: int, today: date) -> set[tuple[object, ...]]:
    return {
        (event.worker.pk, event.kind, event.occurred_on, event.source_key)
        for event in periodic_report.special_events(fiscal_year, today=today)
    }


# --- 受け入れ確認 ----------------------------------------------------------------------------


def test_phase2_gate(
    page: Page,
    live_server: LiveServer,
    sign_in: SignIn,
    seeded: list[ForeignWorker],
    base_date: date,
    admin_requests: list[str],
) -> None:
    page.set_viewport_size({"width": 1366, "height": 900})
    today = base_date
    numbers = {worker.pk: number for number, worker in enumerate(seeded, start=1)}
    targets = interview_targets(today)
    assert targets <= set(numbers), "seed の外国人以外に定期面談の対象がいる"

    # seed 直後: 対象の全員に、開いている定期面談のタスクがちょうど 1 件ずつあり、
    # 起算日が seed の場面どおり（期限は起算日から deadlines で計算した日）
    open_tasks = list(
        Filing.objects.filter(kind=FilingKind.PERIODIC_INTERVIEW, status__in=OPEN).order_by("pk")
    )
    assert Counter(task.worker_id for task in open_tasks) == Counter(targets)
    expected: dict[int, deadlines.Deadline] = {}
    for task in open_tasks:
        assert task.worker is not None
        basis = expected_basis(numbers[task.worker.pk], task.worker, today)
        assert task.basis_on == basis, numbers[task.worker.pk]
        expected[task.pk] = deadlines.periodic_interview(basis)
        assert filings.deadline_of(task) == expected[task.pk], numbers[task.worker.pk]

    fiscal_year = today.year if today.month >= 4 else today.year - 1
    events_before = special_event_keys(fiscal_year, today)
    reports_before = periodic_report.support_overview(fiscal_year).reports
    interviews_before = set(PeriodicInterview.objects.values_list("pk", flat=True))
    records_before = set(SupportRecord.objects.values_list("pk", flat=True))
    consultations_before = set(Consultation.objects.values_list("pk", flat=True))

    # 担当者ごとに、自分の担当のタスクを画面で記録する
    staff_of = {username: Staff.objects.get(user__username=username) for username in USERS}
    assert {task.assignee_id for task in open_tasks} <= {s.pk for s in staff_of.values()}, (
        "dummy_tanto・dummy_tanto2 以外が担当のタスクがある"
    )
    timings = []
    for username in USERS:
        mine = [task for task in open_tasks if task.assignee_id == staff_of[username].pk]
        assert mine, username
        sign_in(User.objects.get(username=username))
        interview_seconds = record_interviews(page, live_server.url, mine, expected)
        own_workers = sorted(
            {task.worker for task in mine if task.worker is not None}, key=lambda w: w.pk
        )
        consultation_seconds = record_consultations(
            page, live_server.url, own_workers[: CONSULTATIONS_PER_USER[username]]
        )
        timings.append(Timing(username, interview_seconds, consultation_seconds))
        sign_out(page)

    # 対象の全員の組（本人用・上司用）を記録した
    recorded = PeriodicInterview.objects.exclude(pk__in=interviews_before)
    assert Counter(recorded.values_list("worker_id", flat=True)) == Counter(targets)
    assert set(recorded.values_list("basis_on", flat=True)) == {today}
    rows = SupportRecord.objects.exclude(pk__in=records_before).filter(
        kind=SupportItem.PERIODIC_INTERVIEW
    )
    assert Counter(rows.values_list("counterpart", flat=True)) == {
        Counterpart.WORKER: len(targets),
        Counterpart.SUPERVISOR: len(targets),
    }

    # 記録したタスクはすべて完了。開いている定期面談のタスクは、基準日を起算日とする次回の分だけ
    for task in open_tasks:
        task.refresh_from_db()
        assert task.status == FilingStatus.COMPLETED, task.pk
    following = Filing.objects.filter(kind=FilingKind.PERIODIC_INTERVIEW, status__in=OPEN)
    assert Counter(following.values_list("worker_id", flat=True)) == Counter(targets)
    for task in following:
        assert filings.deadline_of(task) == deadlines.periodic_interview(today)

    # 相談 5 件（解決 2 件・受付 3 件）
    consultations = Consultation.objects.exclude(pk__in=consultations_before)
    assert Counter(consultations.values_list("status", flat=True)) == {
        ConsultationStatus.RESOLVED: 2,
        ConsultationStatus.RECEIVED: 3,
    }

    # T24 の関数: 期間中の定期面談の実施日に基準日が入る・相談が一覧に出る・
    # 特異事案の候補・通報の有無は変わらない（問題なしの記録だけなので）
    assert targets <= {row.worker.pk for row in periodic_report.support_rows(fiscal_year)}
    overview = periodic_report.support_overview(fiscal_year)
    for worker_id in targets:
        assert today in overview.items[worker_id][SupportItem.PERIODIC_INTERVIEW]
    listed = {summary.consultation.pk for summary in overview.consultations}
    assert set(consultations.values_list("pk", flat=True)) <= listed
    assert special_event_keys(fiscal_year, today) == events_before
    assert overview.reports == reports_before  # 通報の有無は変わらない

    assert admin_requests == []

    # 参考: 操作の時間（合否には使わない）
    print(f"\n段階2の受け入れ確認 基準日 {today}・定期面談の対象 {len(targets)} 人")
    for timing in timings:
        total = sum(timing.interviews)
        print(
            f"  {timing.username}: 定期面談 {len(timing.interviews)} 組 {total:.1f} 秒"
            f"（1 組あたり {total / len(timing.interviews):.1f} 秒）・"
            f"相談 {len(timing.consultations)} 件 {sum(timing.consultations):.1f} 秒"
        )
