"""ダッシュボードの「担当者なし」と「今後の予定」（修正 1 回目 A3）。

- 加入の更新は有効期限の 1 か月前、支援委託契約の更新は終了日の 3 か月前から起票する。それより前の
  期限は「今後の予定」に、まだタスクになっていない期限として出す（今日から 6 か月先まで）。
- 担当者の無いタスク（受入れ機関の期限など）は、担当者で絞った一覧に入らないので「担当者なし」に
  出す。
"""

import datetime

import pytest

from apps.filings.models import FilingKind, Trigger
from apps.hosts.models import HostMembership, SupportContract
from apps.workers.models import WorkerStatus
from services import dashboard, filings
from services.dashboard import DashboardQuery
from tests.contracts.factories import make_contract
from tests.core.factories import make_staff, make_user, verified_client
from tests.hosts.factories import category, field, make_host, make_site
from tests.workers.factories import add_residence, make_worker

pytestmark = pytest.mark.django_db

TODAY = datetime.date(2026, 12, 15)


@pytest.fixture
def host() -> object:
    host = make_host()
    HostMembership.objects.create(
        host_organization=host,
        field=field("industrial_products"),
        joined_on=datetime.date(2025, 1, 1),
        valid_until=datetime.date(2027, 5, 31),  # 受付は 2027-04-30 から
    )
    SupportContract.objects.create(
        host_organization=host,
        concluded_on=datetime.date(2025, 3, 1),
        starts_on=datetime.date(2025, 4, 1),
        ends_on=datetime.date(2027, 3, 31),  # 受付は 2026-12-31 から
    )
    return host


def kinds(rows: list[dashboard.UpcomingRow]) -> list[str]:
    return [row.kind for row in rows]


def test_upcoming_lists_deadlines_that_are_not_tasks_yet(host: object) -> None:
    rows = dashboard.upcoming(DashboardQuery(), TODAY)
    assert kinds(rows) == [FilingKind.SUPPORT_CONTRACT_RENEWAL, FilingKind.MEMBERSHIP_RENEWAL]
    assert rows[0].deadline.opens_on == datetime.date(2026, 12, 31)
    assert rows[0].filing is None and rows[0].target_url_name == "hosts:detail"
    # 6 か月より先の期限は出さない
    assert kinds(dashboard.upcoming(DashboardQuery(), datetime.date(2026, 10, 19))) == [
        FilingKind.SUPPORT_CONTRACT_RENEWAL
    ]
    # 受付の日を過ぎたらタスクになるので、今後の予定からは消える
    later = datetime.date(2027, 1, 5)
    filings.create_scheduled(later)
    assert FilingKind.SUPPORT_CONTRACT_RENEWAL not in kinds(
        dashboard.upcoming(DashboardQuery(), later)
    )


def test_upcoming_follows_the_assignee_of_the_worker(host: object) -> None:
    mine, other = make_staff(name="佐藤"), make_staff(name="鈴木")
    worker = make_worker(assigned_staff=other, status=WorkerStatus.RESIDING)
    add_residence(worker, expires_on=datetime.date(2027, 5, 1))  # 受付は 2027-02-01 から
    query_mine = DashboardQuery(assignee_id=mine.pk)
    assert FilingKind.RESIDENCE_RENEWAL not in kinds(dashboard.upcoming(query_mine, TODAY))
    assert FilingKind.RESIDENCE_RENEWAL in kinds(dashboard.upcoming(DashboardQuery(), TODAY))
    # 外国人に紐づかない期限は、担当者で絞っても出す
    assert FilingKind.MEMBERSHIP_RENEWAL in kinds(dashboard.upcoming(query_mine, TODAY))


def test_unassigned_tasks_are_listed_separately_for_everyone(host: object) -> None:
    filings.create_scheduled(datetime.date(2027, 1, 5))  # 支援委託契約の更新（担当者なし）
    staff = make_staff(name="佐藤")
    site = make_site(host)  # type: ignore[arg-type]
    site.categories.set([category("machine_metal_processing")])
    contract = make_contract(make_worker(assigned_staff=staff), site=site)
    own, _created = filings.create(
        FilingKind.CONTRACT_CHANGE, target=contract, basis_on=TODAY, trigger=Trigger.MANUAL
    )
    query = DashboardQuery(assignee_id=staff.pk)
    assert [row.filing for row in dashboard.dashboard(query, TODAY).rows] == [own]
    assert kinds_of(dashboard.unassigned(query, TODAY)) == [FilingKind.SUPPORT_CONTRACT_RENEWAL]
    assert dashboard.unassigned(DashboardQuery(), TODAY) == []  # 全員の一覧には入っている
    # 画面: 自分の担当の担当者に「担当者なし」と「今後の予定」が出る
    user = make_user("tanto", roles=("support_staff",))
    staff.user = user
    staff.save()
    page = verified_client(user).get("/").content.decode()
    assert 'id="dashboard-unassigned"' in page and 'id="dashboard-upcoming"' in page


def kinds_of(rows: list[dashboard.FilingRow]) -> list[str]:
    return [row.filing.kind for row in rows]
