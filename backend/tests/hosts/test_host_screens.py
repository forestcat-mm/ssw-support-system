"""受入れ機関の一覧・詳細（T13e）: 期限・所属人数・画面の権限・閲覧の監査。"""

from datetime import date, timedelta

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.files.base import ContentFile
from django.test import Client
from django.utils import timezone

from apps.core.models import Attachment, AuditAction, AuditLog
from apps.hosts.models import (
    HostCooperationConfirmation,
    HostMembership,
    HostOrganization,
    SupportContract,
)
from services import deadlines, host_detail, workers
from tests.conftest import PDF_BYTES
from tests.contracts.factories import make_contract
from tests.core.factories import make_user, verified_client
from tests.hosts.factories import field, make_host, make_site
from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 16)


def attach(
    target: object, valid_until: date | None, kind: str = "cooperation_confirmation"
) -> Attachment:
    return Attachment.objects.create(
        kind=kind,
        file=ContentFile(PDF_BYTES, name="file.pdf"),
        original_filename="file.pdf",
        size=len(PDF_BYTES),
        sha256="0" * 64,
        valid_until=valid_until,
        content_type=ContentType.objects.get_for_model(target),  # type: ignore[arg-type]
        object_id=target.pk,  # type: ignore[attr-defined]
    )


@pytest.fixture
def host() -> HostOrganization:
    host = make_host()
    site = make_site(host)
    make_site(host, name="第二工場", field_code="food_manufacturing", is_active=False)
    HostMembership.objects.create(
        host_organization=host,
        field=field("industrial_products"),
        joined_on=date(2025, 4, 1),
        valid_until=TODAY + timedelta(days=5),
    )
    confirmation = HostCooperationConfirmation.objects.create(
        host_organization=host, site=site, municipality="千代田区", submitted_on=date(2025, 4, 1)
    )
    attach(confirmation, TODAY + timedelta(days=12))  # 協力確認書の控えの有効期限は見ない
    SupportContract.objects.create(
        host_organization=host,
        concluded_on=date(2025, 3, 1),
        starts_on=date(2025, 4, 1),
        ends_on=TODAY + timedelta(days=60),
    )
    return host


def test_host_rows(host: HostOrganization) -> None:
    user = make_user("tanto", roles=("support_staff",))
    site = host.sites.get(is_active=True)
    make_contract(make_worker(), site=site)
    deactivated = make_worker(name_romaji="GONE", name_kanji="")
    make_contract(deactivated, site=site, starts_on=date(2026, 5, 1), changed_on=date(2026, 5, 1))
    workers.deactivate(deactivated, by=None, reason="重複")

    (row,) = host_detail.host_rows(user, TODAY)
    assert row.site_count == 1  # 無効の事業所は数えない
    assert [f.code for f in row.fields] == ["industrial_products"]
    assert row.worker_count == 1  # 無効化済みは数えない
    assert row.membership_deadline is not None
    assert deadlines.assess(row.membership_deadline, TODAY).level is deadlines.Level.WITHIN_7
    assert row.sites_without_cooperation == 0  # 有効な事業所は提出済み（無効の事業所は数えない）
    assert row.support_contract_deadline is not None
    assert row.support_contract_deadline.unadjusted_due_on == TODAY + timedelta(days=60)


def test_support_contract_for_prefers_active_then_upcoming_then_last() -> None:
    host = make_host()
    old = SupportContract.objects.create(
        host_organization=host,
        concluded_on=date(2024, 3, 1),
        starts_on=date(2024, 4, 1),
        ends_on=date(2025, 3, 31),
    )
    assert host_detail.support_contract_for([old], TODAY) == old
    upcoming = SupportContract(host_organization=host, starts_on=date(2026, 11, 1))
    assert host_detail.support_contract_for([old, upcoming], TODAY) == upcoming
    active = SupportContract(host_organization=host, starts_on=date(2025, 4, 1))
    assert host_detail.support_contract_for([old, upcoming, active], TODAY) == active


def test_host_list_page_is_audited(support_staff_client: Client, host: HostOrganization) -> None:
    page = support_staff_client.get("/hosts/")
    assert page.status_code == 200
    html = page.content.decode()
    assert f'href="/hosts/{host.pk}/"' in html
    assert 'data-level="within_7"' in html
    entry = AuditLog.objects.filter(action=AuditAction.VIEW).order_by("-pk").first()
    assert entry is not None and entry.context["screen"] == "hosts:list"
    assert entry.context["object_ids"] == [host.pk]


def test_host_detail_shows_sections_and_workers(
    support_staff_client: Client, host: HostOrganization
) -> None:
    worker = make_worker()
    make_contract(worker, site=host.sites.get(is_active=True))
    page = support_staff_client.get(f"/hosts/{host.pk}/")
    html = page.content.decode()
    for section in ("host-basic", "sites", "memberships", "cooperation", "support-contracts"):
        assert f'id="{section}"' in html
    assert f'href="/workers/{worker.pk}/"' in html
    assert "千代田区" in html
    assert "/admin/" not in html.split("<main")[1]  # admin へのリンクは支援担当者に出さない
    entry = AuditLog.objects.filter(action=AuditAction.VIEW).order_by("-pk").first()
    assert entry is not None
    assert entry.object_id == str(host.pk)
    assert entry.context["shown"]["workers"] == [worker.pk]


def test_detail_has_no_admin_link_and_edit_links_for_managers(
    admin_client: Client, host: HostOrganization
) -> None:
    """登録・編集は業務画面で行う（T28a）。管理者にも管理画面へのリンクを出さない。"""
    html = admin_client.get(f"/hosts/{host.pk}/").content.decode()
    assert "/admin/" not in html.split("<main")[1]
    assert f'href="/hosts/{host.pk}/edit/"' in html
    assert f'href="/hosts/{host.pk}/sites/new/"' in html


def test_edit_links_need_hosts_manage(host: HostOrganization) -> None:
    user = make_user("viewer", roles=("support_staff",))
    user.user_roles.get().role.permissions.filter(action="hosts.manage").delete()
    client = verified_client(user)
    html = client.get(f"/hosts/{host.pk}/").content.decode()
    assert f'href="/hosts/{host.pk}/edit/"' not in html
    assert "/hosts/new/" not in client.get("/hosts/").content.decode()
    assert client.get(f"/hosts/{host.pk}/edit/").status_code == 403
    assert client.get("/hosts/new/").status_code == 403


def test_workers_section_needs_workers_view(host: HostOrganization) -> None:
    user = make_user("hostonly", roles=("support_staff",))
    user.user_roles.get().role.permissions.filter(action="workers.view").delete()
    html = verified_client(user).get(f"/hosts/{host.pk}/").content.decode()
    assert 'id="host-workers"' not in html


def test_host_screens_need_hosts_view(host: HostOrganization) -> None:
    client = verified_client(make_user("norole"))
    assert client.get("/hosts/").status_code == 403
    assert client.get(f"/hosts/{host.pk}/").status_code == 403


def test_category_out_of_effect_is_marked(support_staff_client: Client) -> None:
    host = make_host()
    site = make_site(host, field_code="food_manufacturing")
    future = field("food_manufacturing").categories.filter(valid_from__gt=timezone.localdate())
    if not future.exists():
        pytest.skip("適用開始前の業務区分がない")
    site.categories.set(list(future[:1]))
    html = support_staff_client.get(f"/hosts/{host.pk}/").content.decode()
    assert "適用期間外" in html
