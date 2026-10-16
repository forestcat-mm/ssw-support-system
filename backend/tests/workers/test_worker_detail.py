"""外国人詳細（T13c）: 基本・契約・届出・履歴の組み立てと、画面の権限・監査。"""

from datetime import date

import pytest
from django.db.models import Field
from django.test import Client
from django.utils import timezone

from apps.contracts.models import EmploymentContractVersion
from apps.core.models import AuditAction, AuditLog, User
from apps.filings.models import FilingKind, FilingStatus, Trigger
from apps.workers.models import ForeignWorker
from services import contracts, filings, worker_detail, workers
from tests.contracts.factories import add_version, make_contract, site_with_categories
from tests.core.factories import make_user, verified_client
from tests.workers.factories import add_passport, add_residence, make_worker

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 16)


def label(name: str) -> str:
    model_field = EmploymentContractVersion._meta.get_field(name)
    assert isinstance(model_field, Field)
    return str(model_field.verbose_name)


# --- 基本 -----------------------------------------------------------------------


def test_basic_info_uses_latest_rows_and_lists_decrypted_fields() -> None:
    worker = make_worker()
    add_residence(worker, card_number="AB11111111CD", permitted_on=date(2024, 4, 1))
    latest = add_residence(worker, card_number="AB22222222CD", permitted_on=date(2025, 4, 1))
    passport = add_passport(worker)
    info = worker_detail.basic_info(worker)
    assert info.residence == latest
    assert info.residence_deadline is not None
    assert info.residence_deadline.unadjusted_due_on == latest.expires_on
    assert info.passport == passport
    assert info.address is None and info.contact is None
    assert info.shown_ids() == {"residence_record": latest.pk, "passport_record": passport.pk}
    assert info.decrypted_fields() == {"card_number": "***", "passport_number": "***"}


def test_basic_info_skips_voided_rows() -> None:
    worker = make_worker()
    kept = add_residence(worker, card_number="AB11111111CD", permitted_on=date(2024, 4, 1))
    wrong = add_residence(worker, card_number="AB22222222CD", permitted_on=date(2025, 4, 1))
    workers.void_record(wrong, by=None, reason="入力の誤り")
    assert worker_detail.basic_info(worker).residence == kept


# --- 契約 -----------------------------------------------------------------------


def test_default_contract_prefers_active_then_scheduled_then_last_terminated() -> None:
    site = site_with_categories()
    worker = make_worker()
    old = make_contract(worker, site=site, starts_on=date(2025, 4, 1), ends_on=date(2026, 3, 31))
    contracts.terminate(old, on=date(2026, 3, 31), reason="expiry", by=None)
    assert worker_detail.default_contract(worker_detail.contracts_of(worker), TODAY) == old

    scheduled = make_contract(
        worker, site=site, starts_on=date(2026, 11, 1), ends_on=None, changed_on=date(2026, 10, 1)
    )
    assert worker_detail.default_contract(worker_detail.contracts_of(worker), TODAY) == scheduled

    active = make_contract(
        worker, site=site, starts_on=date(2026, 4, 1), ends_on=date(2026, 10, 31)
    )
    assert worker_detail.default_contract(worker_detail.contracts_of(worker), TODAY) == active


def test_contract_view_shows_latest_version_and_diff_from_previous() -> None:
    contract = make_contract(make_worker())
    add_version(contract, base_wage=230000, changed_on=date(2026, 7, 1))
    view = worker_detail.contract_view(contract, None, TODAY)
    assert view.version.version == 2
    assert view.previous is not None and view.previous.version == 1
    rows = {row.label: (row.before, row.after) for row in view.diff}
    assert rows[label("base_wage")] == ("¥220,000", "¥230,000")
    # テスト用の add_version は手当・控除を引き継がない（差分にも「手当」「控除」が出る）
    assert view.monthly_total == 230000
    assert view.take_home == 230000
    assert rows[str(worker_detail.DETAIL_LABELS["allowances"])] == ("住宅手当 ¥10,000", "—")

    first = worker_detail.contract_view(contract, 1, TODAY)
    assert first.version.version == 1 and first.previous is None and first.diff == []
    # 存在しない版は最新に戻す
    assert worker_detail.contract_view(contract, 99, TODAY).version.version == 2


def test_diff_rows_format_choices_booleans_and_details() -> None:
    contract = make_contract(make_worker())
    add_version(contract, wage_type="hourly", has_bonus=True, changed_on=date(2026, 7, 1))
    latest = contracts.latest_version(contract)
    assert latest is not None
    previous = contracts.previous_version(latest)
    assert previous is not None
    rows = {row.label: (row.before, row.after) for row in worker_detail.diff_rows(previous, latest)}
    assert rows[label("wage_type")] == ("月給", "時間給")
    assert rows[label("has_bonus")] == ("なし", "あり")


# --- 届出 -----------------------------------------------------------------------


def test_worker_filings_puts_open_tasks_first_by_due_date() -> None:
    worker = make_worker()
    contract = make_contract(worker)
    late, _ = filings.create(
        FilingKind.CONTRACT_CHANGE,
        target=contract,
        basis_on=date(2026, 10, 10),
        trigger=Trigger.MANUAL,
    )
    soon, _ = filings.create(
        FilingKind.CONTRACT_TERMINATION,
        target=contract,
        basis_on=date(2026, 10, 1),
        trigger=Trigger.MANUAL,
    )
    done, _ = filings.create(
        FilingKind.CONTRACT_NEW, target=contract, basis_on=date(2026, 4, 1), trigger=Trigger.MANUAL
    )
    filings.transition(done, FilingStatus.SUBMITTED, by=None, submitted_on=date(2026, 4, 10))
    rows = worker_detail.worker_filings(worker)
    assert [row.filing for row in rows] == [soon, late, done]
    assert [row.is_open for row in rows] == [True, True, False]
    assert rows[0].filer_label == "受入れ機関"


# --- 履歴 -----------------------------------------------------------------------


def test_mask_number() -> None:
    assert worker_detail.mask_number("AB12345678CD") == "AB********CD"
    assert worker_detail.mask_number("E1234") == "E1*34"
    assert worker_detail.mask_number("ABC") == "***"
    assert worker_detail.mask_number("") == ""


def test_history_merges_kinds_newest_first_with_voided_rows() -> None:
    worker = make_worker()
    old = add_residence(worker, card_number="AB11111111CD", permitted_on=date(2024, 4, 1))
    new = add_residence(worker, card_number="AB22222222CD", permitted_on=date(2025, 4, 1))
    passport = add_passport(worker, issued_on=date(2024, 10, 1))
    workers.void_record(old, by=None, reason="番号の誤り")
    entries = worker_detail.history(worker)
    assert [entry.record for entry in entries] == [new, passport, old]
    assert entries[0].masked_number == "AB********CD"
    assert entries[2].voided and entries[2].record.void_reason == "番号の誤り"
    assert all("AB22222222CD" not in str(entry.details) for entry in entries)


# --- 画面 -----------------------------------------------------------------------


@pytest.fixture
def worker() -> ForeignWorker:
    worker = make_worker()
    add_residence(worker, card_number="AB12345678CD", expires_on=date(2027, 4, 1))
    add_passport(worker, number="E12345678")
    return worker


def last_view_log() -> AuditLog:
    entry = AuditLog.objects.filter(action=AuditAction.VIEW).order_by("-pk").first()
    assert entry is not None
    return entry


def test_basic_tab_shows_decrypted_numbers_and_audits_without_values(
    support_staff_client: Client, worker: ForeignWorker
) -> None:
    before = AuditLog.objects.filter(action=AuditAction.VIEW).count()
    html = support_staff_client.get(f"/workers/{worker.pk}/").content.decode()
    assert "AB12345678CD" in html and "E12345678" in html
    assert AuditLog.objects.filter(action=AuditAction.VIEW).count() == before + 1
    entry = last_view_log()
    assert entry.object_id == str(worker.pk)
    assert entry.context["tab"] == "basic"
    assert entry.context["decrypted"] == {"card_number": "***", "passport_number": "***"}
    assert "AB12345678CD" not in str(entry.context) and "E12345678" not in str(entry.context)


def test_history_tab_masks_numbers(support_staff_client: Client, worker: ForeignWorker) -> None:
    html = support_staff_client.get(f"/workers/{worker.pk}/history/").content.decode()
    assert "AB********CD" in html and "AB12345678CD" not in html
    assert "data-number-actions" in html  # 段階2の「全桁を表示」の置き場
    assert last_view_log().context["tab"] == "history"


def test_contracts_tab_audits_shown_contract(
    support_staff_client: Client, worker: ForeignWorker
) -> None:
    site = site_with_categories()
    contract = make_contract(worker, site=site)
    page = support_staff_client.get(f"/workers/{worker.pk}/contracts/")
    assert page.status_code == 200
    assert "テスト工業株式会社" in page.content.decode()
    assert last_view_log().context["shown"]["contract"] == contract.pk
    # ほかの外国人の契約は指定しても出さない
    other = make_contract(make_worker(name_romaji="OTHER", name_kanji=""), site=site)
    page = support_staff_client.get(f"/workers/{worker.pk}/contracts/?contract={other.pk}")
    assert page.context["view"].contract == contract


def test_filings_tab(support_staff_client: Client, worker: ForeignWorker) -> None:
    contract = make_contract(worker)
    filings.create(
        FilingKind.CONTRACT_CHANGE,
        target=contract,
        basis_on=timezone.localdate(),
        trigger=Trigger.MANUAL,
    )[0]
    html = support_staff_client.get(f"/workers/{worker.pk}/filings/").content.decode()
    assert "雇用契約変更届出" in html or "雇用契約" in html
    assert 'data-level="within_14"' in html


def test_detail_requires_permission_and_returns_404_for_unknown(
    support_staff_client: Client, worker: ForeignWorker
) -> None:
    assert verified_client(make_user("norole")).get(f"/workers/{worker.pk}/").status_code == 403
    assert support_staff_client.get("/workers/999999/").status_code == 404


def test_tabs_needing_other_actions_are_hidden(worker: ForeignWorker) -> None:
    viewer = make_user("viewer", roles=("support_staff",))
    viewer_role = viewer.user_roles.get().role
    viewer_role.permissions.filter(action__in=["contracts.view", "filings.view"]).delete()
    client = verified_client(viewer)
    html = client.get(f"/workers/{worker.pk}/").content.decode()
    assert f"/workers/{worker.pk}/contracts/" not in html
    assert client.get(f"/workers/{worker.pk}/contracts/").status_code == 403


def test_back_to_list_keeps_conditions(support_staff_client: Client, worker: ForeignWorker) -> None:
    support_staff_client.get("/workers/?status=all&order=name")
    html = support_staff_client.get(f"/workers/{worker.pk}/").content.decode()
    assert 'href="/workers/?status=all&amp;order=name"' in html


def test_list_links_to_detail(support_staff_client: Client, worker: ForeignWorker) -> None:
    html = support_staff_client.get("/workers/").content.decode()
    assert f'href="/workers/{worker.pk}/"' in html


def test_admin_user_sees_all_tabs(admin_user: User, worker: ForeignWorker) -> None:
    html = verified_client(admin_user).get(f"/workers/{worker.pk}/").content.decode()
    for suffix in ("contracts/", "filings/", "history/"):
        assert f"/workers/{worker.pk}/{suffix}" in html
