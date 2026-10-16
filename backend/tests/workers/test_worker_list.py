"""外国人一覧（T13b）: 検索・絞り込み・並べ替え・受入れ機関の決め方・クエリの数・画面・監査。"""

from datetime import date
from typing import Any

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.core.models import AuditAction, AuditLog, User
from apps.workers.models import WorkerStatus
from services import contracts, deadlines
from services.worker_list import Order, WorkerListQuery, list_workers, normalize_name
from tests.contracts.factories import make_contract, site_with_categories
from tests.core.factories import make_staff, make_user, verified_client
from tests.hosts.factories import field, make_host, make_site
from tests.workers.factories import add_passport, add_residence, make_worker

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 16)


@pytest.fixture
def user() -> User:
    return make_user("tanto", roles=("support_staff",))


def names(user: User, **query: Any) -> list[str]:
    result = list_workers(user, WorkerListQuery(**query), TODAY)
    return [row.worker.name_romaji for row in result.rows]


def test_normalize_name() -> None:
    assert normalize_name("ｗａｎｇ　Ｆａｎｇ") == "WANGFANG"
    assert normalize_name(" 王 芳 ") == "王芳"


def test_search_by_name_in_three_scripts_ignoring_width_case_and_spaces(user: User) -> None:
    make_worker(name_romaji="WANG FANG", name_kanji="王芳", name_native="王芳")
    make_worker(name_romaji="NGUYEN VAN AN", name_kanji="", name_native="Nguyễn Văn An")
    assert names(user, text="ｗａｎｇ　ｆａｎｇ") == ["WANG FANG"]
    assert names(user, text="wangfang") == ["WANG FANG"]
    assert names(user, text="王 芳") == ["WANG FANG"]
    assert names(user, text="Văn") == ["NGUYEN VAN AN"]
    assert names(user, text="van an") == ["NGUYEN VAN AN"]


def test_search_by_document_number_is_exact_match_only(user: User) -> None:
    wang = make_worker()
    add_residence(wang, card_number="AB12345678CD")
    li = make_worker(name_romaji="LI NA", name_kanji="李娜")
    add_passport(li, number="E98765432")
    assert names(user, text="ab 1234 5678 cd") == ["WANG FANG"]
    assert names(user, text="Ｅ９８７６５４３２") == ["LI NA"]
    assert names(user, text="AB1234") == []  # 部分一致はしない


def test_default_statuses_are_before_entry_and_residing(user: User) -> None:
    for romaji, status in [
        ("A PRE", WorkerStatus.PRE_ENTRY),
        ("B RESIDING", WorkerStatus.RESIDING),
        ("C LEFT", WorkerStatus.LEFT_JOB),
        ("D RETURNED", WorkerStatus.RETURNED),
    ]:
        make_worker(name_romaji=romaji, name_kanji="", status=status)
    assert names(user, order=Order.NAME) == ["A PRE", "B RESIDING"]
    assert names(user, order=Order.NAME, statuses=()) == [
        "A PRE",
        "B RESIDING",
        "C LEFT",
        "D RETURNED",
    ]
    assert names(user, statuses=(WorkerStatus.LEFT_JOB,)) == ["C LEFT"]


def test_filter_by_assigned_staff(user: User) -> None:
    sato = make_staff(name="佐藤")
    make_worker(name_romaji="ASSIGNED", name_kanji="", assigned_staff=sato)
    make_worker(name_romaji="UNASSIGNED", name_kanji="")
    assert names(user, assigned_staff_id=sato.pk) == ["ASSIGNED"]
    assert names(user, unassigned_only=True) == ["UNASSIGNED"]


def test_order_by_expiry_puts_workers_without_residence_last(user: User) -> None:
    late = make_worker(name_romaji="LATE", name_kanji="")
    add_residence(late, card_number="AB11111111CD", expires_on=date(2027, 6, 1))
    soon = make_worker(name_romaji="SOON", name_kanji="")
    add_residence(soon, card_number="AB22222222CD", expires_on=date(2026, 10, 20))
    make_worker(name_romaji="ALREADY ABROAD", name_kanji="", status=WorkerStatus.PRE_ENTRY)
    assert names(user) == ["SOON", "LATE", "ALREADY ABROAD"]
    assert names(user, order=Order.NAME) == ["ALREADY ABROAD", "LATE", "SOON"]


def test_levels_are_counted_and_filtered(user: User) -> None:
    for romaji, card, expires_on in [
        ("OVERDUE", "AB00000001CD", date(2026, 10, 11)),
        ("WEEK", "AB00000002CD", date(2026, 10, 19)),
        ("TWO WEEKS", "AB00000003CD", date(2026, 10, 28)),
        ("LATER", "AB00000004CD", date(2027, 10, 1)),
    ]:
        add_residence(
            make_worker(name_romaji=romaji, name_kanji=""), card_number=card, expires_on=expires_on
        )
    result = list_workers(user, WorkerListQuery(), TODAY)
    assert result.level_counts[deadlines.Level.OVERDUE] == 1
    assert result.level_counts[deadlines.Level.WITHIN_7] == 1
    assert result.level_counts[deadlines.Level.WITHIN_14] == 1
    assert names(user, level=deadlines.Level.WITHIN_7) == ["WEEK"]
    # 絞り込んでも見出しの人数は変わらない（押して切り替えるため）
    filtered = list_workers(user, WorkerListQuery(level=deadlines.Level.WITHIN_7), TODAY)
    assert filtered.level_counts[deadlines.Level.OVERDUE] == 1


def test_latest_residence_record_decides_expiry(user: User) -> None:
    worker = make_worker()
    add_residence(worker, card_number="AB11111111CD", expires_on=date(2026, 10, 20))
    add_residence(
        worker,
        card_number="AB22222222CD",
        permitted_on=date(2026, 9, 1),
        expires_on=date(2027, 9, 1),
    )
    (row,) = list_workers(user, WorkerListQuery(), TODAY).rows
    assert row.expires_on == date(2027, 9, 1)
    assert row.level is deadlines.Level.NOT_YET


def test_host_organization_is_active_then_scheduled_then_none(user: User) -> None:
    food_host = make_host(name="食品株式会社", corporate_number="7999900000010")
    food_site = make_site(food_host, field_code="food_manufacturing")
    industrial_site = site_with_categories()  # テスト工業株式会社（工業）
    active = make_worker(name_romaji="ACTIVE", name_kanji="")
    make_contract(active, site=industrial_site)  # 2026-04-01〜
    scheduled = make_worker(name_romaji="SCHEDULED", name_kanji="")
    make_contract(
        scheduled, site=food_site, starts_on=date(2026, 11, 1), changed_on=date(2026, 10, 1)
    )
    ended = make_worker(name_romaji="ENDED", name_kanji="")
    contracts.terminate(
        make_contract(ended, site=industrial_site),
        on=date(2026, 9, 30),
        reason="voluntary",
        by=None,
    )
    make_worker(name_romaji="NONE", name_kanji="")

    rows = {
        row.worker.name_romaji: row for row in list_workers(user, WorkerListQuery(), TODAY).rows
    }
    assert rows["ACTIVE"].host_organization_name == "テスト工業株式会社"
    assert rows["ACTIVE"].skill_field is not None
    assert rows["ACTIVE"].skill_field.code == "industrial_products"
    assert not rows["ACTIVE"].contract_scheduled
    assert rows["SCHEDULED"].host_organization_name == "食品株式会社"
    assert rows["SCHEDULED"].contract_scheduled
    assert rows["ENDED"].host_organization_name == ""
    assert rows["NONE"].skill_field is None

    assert names(user, host_organization_id=food_host.pk) == ["SCHEDULED"]
    assert names(user, field_id=field("industrial_products").pk) == ["ACTIVE"]


def test_display_name_prefers_kanji_then_native_with_language(user: User) -> None:
    make_worker(name_romaji="WANG FANG", name_kanji="王芳", name_native="王芳")
    make_worker(name_romaji="LI NA", name_kanji="", name_native="李娜")
    rows = {
        row.worker.name_romaji: row for row in list_workers(user, WorkerListQuery(), TODAY).rows
    }
    assert (rows["WANG FANG"].display_name, rows["WANG FANG"].display_name_lang) == ("王芳", "")
    assert (rows["LI NA"].display_name, rows["LI NA"].display_name_lang) == ("李娜", "zh-Hans")
    assert rows["LI NA"].romaji == "LI NA"


def test_display_name_falls_back_to_romaji_once(user: User) -> None:
    make_worker(name_romaji="SITI RAHAYU", name_kanji="", name_native="")
    (row,) = list_workers(user, WorkerListQuery(), TODAY).rows
    assert (row.display_name, row.romaji) == ("SITI RAHAYU", "")


def test_number_of_queries_does_not_grow_with_workers(user: User) -> None:
    def count_queries() -> int:
        with CaptureQueriesContext(connection) as context:
            list_workers(user, WorkerListQuery(), TODAY)
        return len(context.captured_queries)

    site = site_with_categories()
    make_contract(make_worker(name_romaji="FIRST", name_kanji=""), site=site)
    baseline = count_queries()
    for index in range(5):
        worker = make_worker(name_romaji=f"WORKER {index}", name_kanji="")
        add_residence(worker, card_number=f"AB{index:08d}CD")
        make_contract(worker, site=site)
    assert count_queries() == baseline


# --- 画面 -----------------------------------------------------------------------


def test_list_page_for_support_staff(support_staff_client: Client) -> None:
    worker = make_worker()
    add_residence(worker)
    page = support_staff_client.get("/workers/")
    assert page.status_code == 200
    html = page.content.decode()
    assert "王芳" in html and "WANG FANG" in html
    assert f'data-worker="{worker.pk}"' in html
    assert 'aria-current="page"' in html  # ナビの「外国人台帳」


def test_list_page_requires_permission() -> None:
    assert verified_client(make_user("norole")).get("/workers/").status_code == 403


def test_list_page_shows_empty_state(support_staff_client: Client) -> None:
    html = support_staff_client.get("/workers/?q=ZZZ").content.decode()
    assert "条件に合う外国人はいません" in html


def test_invalid_conditions_fall_back_to_defaults(support_staff_client: Client) -> None:
    make_worker()
    page = support_staff_client.get("/workers/?host=999999&status=bogus&level=x&order=y")
    assert page.status_code == 200
    assert len(page.context["rows"]) == 1


def test_assigned_to_me(support_staff_client: Client, support_staff_user: User) -> None:
    mine = make_staff(name="自分", user=support_staff_user)
    make_worker(name_romaji="MINE", name_kanji="", assigned_staff=mine)
    make_worker(name_romaji="OTHER", name_kanji="", assigned_staff=make_staff(name="他の人"))
    rows = support_staff_client.get("/workers/?staff=mine").context["rows"]
    assert [row.worker.name_romaji for row in rows] == ["MINE"]


def test_list_view_is_audited_once_with_masked_number(support_staff_client: Client) -> None:
    worker = make_worker()
    add_residence(worker, card_number="AB12345678CD")
    before = AuditLog.objects.filter(action=AuditAction.VIEW).count()
    support_staff_client.get("/workers/?q=AB12345678CD&status=all")
    logs = AuditLog.objects.filter(action=AuditAction.VIEW).order_by("-pk")
    assert logs.count() == before + 1
    entry = logs.first()
    assert entry is not None
    assert entry.context["screen"] == "workers:list"
    assert entry.context["conditions"] == {"q": "***", "status": "all"}
    assert entry.context["count"] == 1
    assert entry.context["object_ids"] == [worker.pk]
    assert "AB12345678CD" not in str(entry.context)


def test_name_search_is_kept_in_audit(support_staff_client: Client) -> None:
    support_staff_client.get("/workers/?q=WANG")
    entry = AuditLog.objects.filter(action=AuditAction.VIEW).order_by("-pk").first()
    assert entry is not None
    assert entry.context["conditions"] == {"q": "WANG"}
