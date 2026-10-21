"""支援委託契約の届出タスク（T28c、ADR-0017）: 対象の外国人、締結・続く行・新しい締結、変更、終了と
その取消、実際の終了日と期間の重なり、更新のタスク、雇用契約の終了の c 欄。"""

from datetime import date, timedelta

import pytest
from django.core.exceptions import ValidationError

from apps.filings.models import Filing, FilingKind, FilingStatus
from apps.hosts.models import HostOrganization, HostSite, SupportContract
from apps.workers.models import ForeignWorker, StatusOfResidence
from services import contracts, filings, host_records, hosts, workers
from tests.contracts.factories import make_contract, site_with_categories
from tests.hosts.factories import make_host
from tests.workers.factories import add_residence, make_worker

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 16)
END_KINDS = (FilingKind.SUPPORT_CONTRACT_END_OR_CONCLUSION, FilingKind.SUPPORT_PLAN_CHANGE)


def employ(
    host_site: HostSite,
    *,
    name: str,
    status: str = StatusOfResidence.SSW_1,
    card: str = "AB12345678CD",
    starts_on: date = date(2026, 4, 1),
) -> ForeignWorker:
    worker = make_worker(name_romaji=name, name_kanji="", status="residing")
    add_residence(worker, card_number=card, status_of_residence=status)
    make_contract(worker, site=host_site, starts_on=starts_on, changed_on=starts_on, ends_on=None)
    return worker


@pytest.fixture
def site() -> HostSite:
    return site_with_categories()


@pytest.fixture
def host(site: HostSite) -> HostOrganization:
    return site.host_organization


@pytest.fixture
def ssw(site: HostSite) -> ForeignWorker:
    return employ(site, name="WANG FANG")


def register(host: HostOrganization, **values: object) -> host_records.SupportSaved:
    contract = SupportContract(
        host_organization=host,
        **(
            {
                "concluded_on": date(2026, 9, 1),
                "starts_on": date(2026, 9, 1),
                "ends_on": date(2027, 8, 31),
                "monthly_fee": 30000,
            }
            | values
        ),
    )
    return host_records.save_support_contract(contract, by=None, today=TODAY)


def kinds_of(worker: ForeignWorker) -> list[str]:
    return sorted(Filing.objects.filter(worker=worker).values_list("kind", flat=True))


# --- 対象の外国人 ------------------------------------------------------------------


def test_targets_are_ssw_1_workers_with_an_effective_contract(
    site: HostSite, host: HostOrganization, ssw: ForeignWorker
) -> None:
    second = employ(site, name="SSW TWO", status=StatusOfResidence.SSW_2, card="AB12345679CD")
    intern = employ(
        site, name="INTERN", status=StatusOfResidence.TECHNICAL_INTERN_3, card="AB12345680CD"
    )
    left = employ(site, name="LEFT", card="AB12345681CD")
    contracts.terminate(
        left.employment_contracts.get(), on=date(2026, 8, 31), reason="voluntary", by=None
    )
    gone = employ(site, name="GONE", card="AB12345682CD")
    workers.deactivate(gone, by=None, reason="重複")
    later = employ(site, name="LATER", card="AB12345683CD", starts_on=date(2026, 12, 1))
    other_host_worker = employ(
        site_with_categories_for(make_host(name="別の会社", corporate_number="7999900000010")),
        name="OTHER",
        card="AB12345684CD",
    )

    targets = hosts.support_targets(host, date(2026, 9, 1))
    assert targets == [ssw]
    for excluded in (second, intern, left, gone, later, other_host_worker):
        assert excluded not in targets


def site_with_categories_for(host: HostOrganization) -> HostSite:
    from tests.hosts.factories import category, make_site

    site = make_site(host)
    site.categories.set([category("machine_metal_processing")])
    return site


# --- 締結・続く行・新しい締結 ----------------------------------------------------------


def test_new_conclusion_raises_3_3_2_and_3_2_for_each_worker(
    host: HostOrganization, ssw: ForeignWorker
) -> None:
    raised = register(host).raised
    assert raised.workers == [ssw]
    assert kinds_of(ssw) == sorted(END_KINDS)
    task = Filing.objects.get(worker=ssw, kind=FilingKind.SUPPORT_CONTRACT_END_OR_CONCLUSION)
    assert task.basis_on == date(2026, 9, 1)  # 締結日
    assert task.trigger_details["event"] == "concluded"
    assert any("B 欄" in note for note in filings.task_notes(task, TODAY))


def test_first_contract_before_any_ssw_worker_raises_nothing(host: HostOrganization) -> None:
    """申請の前の締結（対象の外国人がいない）は届出が要らない（運用要領 本体 印刷 100頁）。"""
    assert register(host).raised.created == []
    assert not Filing.objects.filter(kind__in=END_KINDS).exists()


def test_continuing_row_is_not_a_conclusion_and_raises_3_3_1_only_for_a_new_fee(
    host: HostOrganization, ssw: ForeignWorker
) -> None:
    register(host)  # 2026-09-01〜2027-08-31
    before = Filing.objects.count()
    same_fee = register(
        host,
        concluded_on=date(2027, 8, 1),
        starts_on=date(2027, 9, 1),
        ends_on=date(2028, 8, 31),
    )
    assert same_fee.raised.created == []  # 続く行・同じ委託料: 起票しない
    assert Filing.objects.count() == before

    new_fee = register(
        host,
        concluded_on=date(2028, 8, 1),
        starts_on=date(2028, 9, 1),
        ends_on=date(2029, 8, 31),
        monthly_fee=35000,
    )
    (task,) = new_fee.raised.created
    assert task.kind == FilingKind.SUPPORT_CONTRACT_CHANGE
    assert task.basis_on == date(2028, 9, 1)  # 続く行の開始日
    assert task.trigger_details["changes"] == {"monthly_fee": [30000, 35000]}


def test_row_after_a_terminated_row_or_a_gap_is_a_new_conclusion(
    host: HostOrganization, ssw: ForeignWorker
) -> None:
    first = register(host).raised  # 新しい締結
    assert len(first.created) == 2
    contract = SupportContract.objects.get()
    host_records.terminate_support_contract(
        contract,
        terminated_on=date(2027, 3, 31),
        category="host",
        reason="business",
        note="",
        by=None,
        today=TODAY,
    )
    # 終了を登録した行の後（予定の終了日の翌日に始まっても続く行ではない）
    after_termination = register(
        host, concluded_on=date(2027, 9, 1), starts_on=date(2027, 9, 1), ends_on=None
    )
    events = {t.trigger_details["event"] for t in after_termination.raised.created}
    assert events == {"concluded"}
    assert len(after_termination.raised.created) == 2


def test_gap_between_rows_is_a_new_conclusion(host: HostOrganization, ssw: ForeignWorker) -> None:
    register(host, ends_on=date(2026, 12, 31))
    gap = register(
        host, concluded_on=date(2027, 2, 1), starts_on=date(2027, 2, 1), ends_on=None
    ).raised
    assert {t.kind for t in gap.created} == set(END_KINDS)


# --- 変更 -------------------------------------------------------------------------


def test_amendment_raises_3_3_1_and_a_correction_does_not(
    host: HostOrganization, ssw: ForeignWorker
) -> None:
    register(host)
    contract = SupportContract.objects.get()
    count = Filing.objects.count()
    contract.monthly_fee = 32000
    corrected = host_records.save_support_contract(contract, by=None, today=TODAY)
    assert corrected.raised.created == [] and Filing.objects.count() == count

    contract.monthly_fee = 36000
    amended = host_records.save_support_contract(
        contract, by=None, today=TODAY, amendment=host_records.Amendment(date(2026, 10, 1))
    )
    (task,) = amended.raised.created
    assert task.kind == FilingKind.SUPPORT_CONTRACT_CHANGE
    assert task.basis_on == date(2026, 10, 1)
    assert task.trigger_details["changes"] == {"monthly_fee": [32000, 36000]}
    # 補足は「委託料 32,000 円 → 36,000 円」の形（U3 の直し）
    assert "委託料 32,000 円 → 36,000 円" in filings.task_notes(task, TODAY)[0]


# --- 終了 ------------------------------------------------------------------------


def test_termination_raises_tasks_and_closes_the_renewal_task(
    host: HostOrganization, ssw: ForeignWorker
) -> None:
    register(host, ends_on=TODAY + timedelta(days=30))  # 3 か月以内 → 更新のタスク
    contract = SupportContract.objects.get()
    renewal = Filing.objects.get(kind=FilingKind.SUPPORT_CONTRACT_RENEWAL)
    result = host_records.terminate_support_contract(
        contract,
        terminated_on=TODAY,
        category="support_organization",
        reason="other",
        note="体制の見直し",
        by=None,
        today=TODAY,
    )
    contract.refresh_from_db()
    assert contract.ends_on == TODAY + timedelta(days=30)  # 予定の終了日は残す
    assert contract.effective_ends_on == TODAY
    assert {t.kind for t in result.raised.created} == set(END_KINDS)
    task = next(
        t for t in result.raised.created if t.kind == FilingKind.SUPPORT_CONTRACT_END_OR_CONCLUSION
    )
    assert task.basis_on == TODAY and task.trigger_details["termination_note"] == "体制の見直し"
    assert "A 欄" in filings.task_notes(task, TODAY)[0]
    renewal.refresh_from_db()
    assert renewal.status == FilingStatus.COMPLETED
    # 日次の起票は、終了を登録した契約の更新のタスクを作り直さない
    created = filings.create_scheduled(TODAY).created
    assert [t for t in created if t.kind == FilingKind.SUPPORT_CONTRACT_RENEWAL] == []


@pytest.mark.parametrize(
    ("values", "field"),
    [
        ({"terminated_on": None}, "terminated_on"),
        ({"terminated_on": date(2026, 8, 1)}, "terminated_on"),  # 開始日より前
        ({"terminated_on": date(2027, 9, 1)}, "terminated_on"),  # 予定の終了日より後
        ({"category": ""}, "termination_category"),
        ({"category": "expiry", "reason": "business"}, "termination_reason"),
        ({"reason": "other", "note": ""}, "termination_note"),
        ({"note": "メモ"}, "termination_note"),
    ],
)
def test_termination_is_checked(
    host: HostOrganization, values: dict[str, object], field: str
) -> None:
    register(host)
    contract = SupportContract.objects.get()
    arguments = {
        "terminated_on": date(2027, 3, 31),
        "category": "host",
        "reason": "business",
        "note": "",
    } | values
    with pytest.raises(ValidationError) as error:
        host_records.terminate_support_contract(
            contract,
            by=None,
            today=TODAY,
            **arguments,  # type: ignore[arg-type]
        )
    assert field in error.value.error_dict


def test_actual_end_date_is_the_end_of_the_period_for_overlaps(host: HostOrganization) -> None:
    register(host)  # 予定: 2026-09-01〜2027-08-31
    contract = SupportContract.objects.get()
    host_records.terminate_support_contract(
        contract,
        terminated_on=date(2026, 12, 31),
        category="host",
        reason="business",
        note="",
        by=None,
        today=TODAY,
    )
    # 実際の終了日の後なら、予定の終了日より前に始まる契約を登録できる
    register(host, concluded_on=date(2027, 1, 15), starts_on=date(2027, 2, 1), ends_on=None)
    assert hosts.active_contract(host, date(2027, 1, 15)) is None
    # 終了を取り消すと重なるので取り消せない
    with pytest.raises(ValidationError):
        host_records.cancel_support_termination(contract, reason="誤り", by=None, today=TODAY)


# --- 終了の取消 ----------------------------------------------------------------------


def test_cancelling_the_termination_cancels_untouched_tasks_and_keeps_started_ones(
    site: HostSite, host: HostOrganization, ssw: ForeignWorker
) -> None:
    second = employ(site, name="LI LEI", card="AB12345679CD")
    register(host, ends_on=TODAY + timedelta(days=30))
    contract = SupportContract.objects.get()
    renewal = Filing.objects.get(kind=FilingKind.SUPPORT_CONTRACT_RENEWAL)
    host_records.terminate_support_contract(
        contract,
        terminated_on=TODAY,
        category="host",
        reason="business",
        note="",
        by=None,
        today=TODAY,
    )
    started = Filing.objects.get(
        worker=second,
        kind=FilingKind.SUPPORT_CONTRACT_END_OR_CONCLUSION,
        trigger_details__event="terminated",
    )
    filings.transition(started, FilingStatus.IN_PROGRESS, by=None)

    with pytest.raises(ValidationError):
        host_records.cancel_support_termination(contract, reason=" ", by=None, today=TODAY)
    result = host_records.cancel_support_termination(
        contract, reason="別の受入れ機関に登録した", by=None, today=TODAY
    )
    assert result.raised.left_open == [started]
    terminated_tasks = Filing.objects.filter(trigger_details__event="terminated")
    for task in terminated_tasks.exclude(pk=started.pk):
        assert task.status == FilingStatus.CANCELLED
        assert "別の受入れ機関に登録した" in task.status_reason
    started.refresh_from_db()
    assert started.status == FilingStatus.IN_PROGRESS
    assert (
        "元の事由（支援委託契約の終了）が取り消されました" in filings.task_notes(started, TODAY)[0]
    )
    renewal.refresh_from_db()
    assert renewal.status == FilingStatus.NOT_STARTED  # 終了で完了にした更新のタスクを戻す
    contract.refresh_from_db()
    assert contract.terminated_on is None and contract.termination_category == ""


# --- 表示の注意 ----------------------------------------------------------------------


def test_overdue_renewal_task_tells_what_to_register(host: HostOrganization) -> None:
    register(host, ends_on=TODAY + timedelta(days=10))
    renewal = Filing.objects.get(kind=FilingKind.SUPPORT_CONTRACT_RENEWAL)
    assert filings.task_notes(renewal, TODAY) == []
    assert filings.task_notes(renewal, TODAY + timedelta(days=11)) == [
        "終了日を過ぎています：次の期間か終了を登録"
    ]


def test_employment_termination_notes_column_c_when_support_was_in_effect(
    host: HostOrganization, ssw: ForeignWorker
) -> None:
    register(host)
    contract = ssw.employment_contracts.get()
    contracts.terminate(contract, on=date(2026, 10, 1), reason="voluntary", by=None)
    task = Filing.objects.get(kind=FilingKind.CONTRACT_TERMINATION)
    assert task.trigger_details["support_contract"] == SupportContract.objects.get().pk
    assert any("c 欄" in note for note in filings.task_notes(task, TODAY))


def test_employment_termination_without_support_has_no_note(
    site: HostSite, ssw: ForeignWorker
) -> None:
    contracts.terminate(
        ssw.employment_contracts.get(), on=date(2026, 10, 1), reason="voluntary", by=None
    )
    task = Filing.objects.get(kind=FilingKind.CONTRACT_TERMINATION)
    assert "support_contract" not in task.trigger_details
    assert filings.task_notes(task, TODAY) == []
