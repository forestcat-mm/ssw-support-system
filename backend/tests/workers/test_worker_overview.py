"""外国人の詳細の上の帯と概要のタブ（U3。ADR-0046・仕様 5.2）。"""

from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from apps.contracts.models import EmploymentContract
from apps.core.models import AuditAction, AuditLog
from apps.filings.models import FilingKind, Trigger
from apps.workers.models import ForeignWorker
from services import filings, worker_detail, worker_overview
from tests.contracts.factories import make_contract, site_with_categories
from tests.core.factories import make_user, verified_client
from tests.workers.factories import add_residence, make_worker

pytestmark = pytest.mark.django_db

ALL = worker_overview.Allowed(contracts=True, support=True, filings=True, forms=True)


@pytest.fixture
def contract() -> EmploymentContract:
    worker = make_worker()
    add_residence(worker, expires_on=timezone.localdate() - timedelta(days=5))
    return make_contract(worker, site=site_with_categories())


def overview_of(
    worker: ForeignWorker, allowed: worker_overview.Allowed = ALL
) -> worker_overview.Overview:
    today = timezone.localdate()
    info = worker_detail.basic_info(worker)
    head = worker_overview.header(worker, info, today)
    return worker_overview.overview(worker, head, info, today, allowed)


def test_initial_is_the_first_kanji_or_the_first_letter_of_the_romaji_surname() -> None:
    assert worker_overview.initial_of(make_worker(name_kanji="王芳")) == "王"
    romaji_only = make_worker(name_kanji="", name_romaji="nguyen van an")
    assert worker_overview.initial_of(romaji_only) == "N"


def test_header_shows_the_expiry_level_and_the_host(contract: EmploymentContract) -> None:
    worker = contract.worker
    head = worker_overview.header(worker, worker_detail.basic_info(worker), timezone.localdate())
    assert head.expiry is not None and head.expiry.tone == "danger" and head.expiry.days_left == -5
    assert head.contract == contract
    assert head.residence_status


def test_todo_lists_open_filings_nearest_first(contract: EmploymentContract) -> None:
    worker = contract.worker
    today = timezone.localdate()
    filings.create(
        FilingKind.CONTRACT_CHANGE,
        target=contract,
        basis_on=today - timedelta(days=20),
        trigger=Trigger.MANUAL,
    )
    found = overview_of(worker)
    titles = [item.title for item in found.todos]
    assert "雇用契約変更届出" in titles
    item = next(item for item in found.todos if item.title == "雇用契約変更届出")
    assert item.tone == "danger" and item.state.startswith("超過") and item.section == "contract"
    assert "todo" in found.warnings and "residence" in found.warnings
    assert found.contract is not None and found.contract.version.contract == contract


def test_overview_hides_what_the_viewer_cannot_see(contract: EmploymentContract) -> None:
    filings.create(
        FilingKind.CONTRACT_CHANGE,
        target=contract,
        basis_on=timezone.localdate() - timedelta(days=20),
        trigger=Trigger.MANUAL,
    )
    nothing = worker_overview.Allowed(contracts=False, support=False, filings=False, forms=False)
    found = overview_of(contract.worker, nothing)
    assert found.todos == [] and found.contract is None and found.support is None


def test_overview_page_has_the_header_toc_and_sections(
    support_staff_client: Client, contract: EmploymentContract
) -> None:
    worker = contract.worker
    page = support_staff_client.get(f"/workers/{worker.pk}/")
    html = page.content.decode()
    assert "← 外国人台帳" in html and 'id="person-head"' in html and 'id="person-bar"' in html
    assert 'id="residence-expiry"' in html and "expiry-box--danger" in html
    for section in ("todo", "personal", "residence", "passport", "address", "contract", "support"):
        assert f'href="#{section}"' in html and f'id="{section}"' in html
    tabs = [str(tab["label"]) for tab in page.context["tabs"]]
    assert tabs == ["概要", "契約", "支援", "届出", "帳票", "帳簿", "履歴"]
    # 閲覧の監査ログは画面ごとに 1 件（概要の要約を足しても増えない）。要約に出した契約の版を残す
    logs = AuditLog.objects.filter(
        action=AuditAction.VIEW, content_type__model="foreignworker", object_id=str(worker.pk)
    )
    assert logs.count() == 1
    assert logs.get().context["shown"]["contract_version"] == contract.versions.get().pk


def test_dangerous_actions_are_in_the_menu(admin_client: Client) -> None:
    worker = make_worker()
    html = admin_client.get(f"/workers/{worker.pk}/").content.decode()
    menu = html[html.index('id="person-menu"') :]
    assert "menu__item--danger" in menu[: menu.index("</details>")]
    assert "button--danger" not in html[: html.index('id="person-menu"')]


def test_viewer_without_contract_permission_sees_no_contract_summary(
    contract: EmploymentContract,
) -> None:
    viewer = make_user("viewer2", roles=("support_staff",))
    role = viewer.user_roles.get().role
    role.permissions.filter(action__in=["contracts.view", "filings.view"]).delete()
    html = verified_client(viewer).get(f"/workers/{contract.worker.pk}/").content.decode()
    assert 'id="contract"' not in html and 'href="#contract"' not in html
