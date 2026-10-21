"""受入れ機関の詳細の上の帯・今やること・番号つきのセクション（U7）。"""

from datetime import date

import pytest
from django.test import Client

from apps.filings.models import FilingKind, Trigger
from apps.hosts.models import HostOrganization, SupportContract
from services import dashboard, filings, host_detail, host_overview, host_records
from tests.core.factories import make_user
from tests.hosts.factories import VALID_NUMBERS, make_host, make_site

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 21)


def contract(host: HostOrganization, **values: object) -> SupportContract:
    item = SupportContract(
        host_organization=host,
        **(
            {
                "concluded_on": date(2025, 11, 1),
                "starts_on": date(2025, 11, 1),
                "ends_on": date(2026, 10, 31),
                "monthly_fee": 30000,
            }
            | values
        ),
    )
    host_records.save_support_contract(item, by=None, today=TODAY)
    return item


@pytest.mark.parametrize(
    ("name", "initial"),
    [
        ("（ダミー）サンプル精機工業株式会社", "サ"),
        ("株式会社さくら台フーズ", "さ"),
        ("(dummy) Acme", "A"),
    ],
)
def test_initial_skips_notes_and_entity_types(name: str, initial: str) -> None:
    assert host_overview.initial_of(HostOrganization(name=name)) == initial


def test_header_has_the_main_field_and_the_contract_deadline() -> None:
    host = make_host()
    make_site(host)
    contract(host)
    user = make_user("admin9", roles=("admin",))
    detail = host_detail.host_detail(host, user, TODAY, with_workers=False)
    head = host_overview.header(detail, TODAY)
    assert head.main_field is not None
    assert head.contract is not None and head.contract.deadline is not None
    assert head.contract.tone in {"danger", "caution", "ok", "neutral"}


def test_todos_are_unassigned_open_tasks_of_the_host() -> None:
    host = make_host()
    other = make_host(name="別の機関", corporate_number=VALID_NUMBERS[1])
    mine = contract(host)
    theirs = contract(other)
    for target in (mine, theirs):
        filings.create(
            FilingKind.SUPPORT_CONTRACT_RENEWAL,
            target=target,
            basis_on=date(2026, 10, 31),
            trigger=Trigger.SUPPORT_CONTRACT_END,
        )
    rows = host_overview.todos(host, TODAY)
    kinds = [row.filing.kind for row in rows]
    assert FilingKind.SUPPORT_CONTRACT_RENEWAL in kinds
    assert all(dashboard.host_id_of(row.filing.target) == host.pk for row in rows)


def test_detail_page_has_numbered_sections_and_the_band(admin_client: Client) -> None:
    host = make_host()
    make_site(host)
    html = admin_client.get(f"/hosts/{host.pk}/").content.decode()
    assert 'id="person-head"' in html and 'id="person-bar"' in html
    assert 'id="todo"' in html
    first = '<span class="section-head__no">1</span><h2 class="section-head__title"'
    assert f'{first} id="host-basic-title">' in html
    for section in ("sites", "memberships", "cooperation", "support-contracts", "host-documents"):
        assert f'data-toc-target="{section}"' in html
