"""受入れ機関の登録・編集のサービス（T28a）: 事業所の編集の制限、協力確認書の提出状況、
加入状況・支援委託契約の期限のタスク（保存のときの起票・古い期限の自動の片付け・二重の起票なし）、
移行での協力確認書のタスクの取消。"""

import importlib
from datetime import date, timedelta

import pytest
from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile

from apps.core.models import Attachment, AttachmentKind, AuditAction, AuditLog
from apps.filings.models import Filing, FilingKind, FilingStatus, Trigger
from apps.hosts.models import HostCooperationConfirmation, HostMembership, SupportContract
from services import contracts, filings, host_records, hosts
from tests.conftest import PDF_BYTES
from tests.contracts.factories import make_contract
from tests.core.factories import make_user
from tests.hosts.factories import category, field, make_host, make_site

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 16)


def pdf(name: str = "file.pdf") -> ContentFile[bytes]:
    return ContentFile(PDF_BYTES, name=name)


def tasks(kind: FilingKind, target: object) -> list[Filing]:
    return list(
        Filing.objects.filter(
            kind=kind,
            content_type=ContentType.objects.get_for_model(target),  # type: ignore[arg-type]
            object_id=target.pk,  # type: ignore[attr-defined]
        ).order_by("pk")
    )


def transition_logs(filing: Filing) -> list[AuditLog]:
    return list(
        AuditLog.objects.filter(
            action=AuditAction.FILING_TRANSITION, object_id=str(filing.pk)
        ).order_by("pk")
    )


# --- 事業所の編集の制限（D5）-------------------------------------------------------------


@pytest.fixture
def site_in_use() -> object:
    site = make_site(make_host())
    site.categories.set([category("machine_metal_processing"), category("sewing")])
    make_contract(site=site)  # 現在の版の業務区分は機械金属加工
    return site


def test_site_in_use_keeps_its_field_categories_and_activity(site_in_use: object) -> None:
    site = site_in_use
    site.refresh_from_db()  # type: ignore[attr-defined]
    with pytest.raises(ValidationError) as error:
        hosts.validate_site_change(
            site,  # type: ignore[arg-type]
            field=field("food_manufacturing"),
            categories=[category("sewing")],
            is_active=False,
        )
    assert set(error.value.error_dict) == {"field", "categories", "is_active"}
    assert "機械金属加工" in str(error.value.error_dict["categories"])
    # 使っていない業務区分は外せる
    hosts.validate_site_change(
        site,  # type: ignore[arg-type]
        field=field("industrial_products"),
        categories=[category("machine_metal_processing")],
        is_active=True,
    )


def test_terminated_contract_allows_deactivation_but_not_a_field_change(
    site_in_use: object,
) -> None:
    site = site_in_use
    contract = site.host_organization.employment_contracts.get()  # type: ignore[attr-defined]
    contracts.terminate(contract, on=contract.current_starts_on, reason="expiry", by=None)
    hosts.validate_site_change(
        site,  # type: ignore[arg-type]
        field=field("industrial_products"),
        categories=[],
        is_active=False,
    )
    with pytest.raises(ValidationError) as error:
        hosts.validate_site_change(
            site,  # type: ignore[arg-type]
            field=field("food_manufacturing"),
            categories=[],
            is_active=False,
        )
    assert set(error.value.error_dict) == {"field"}


def test_save_site_reports_resubmission_when_the_address_changes() -> None:
    host = make_host()
    site = make_site(host)
    saved = host_records.save_site(site, [category("sewing")], by=None)
    assert not saved.resubmission_needed
    HostCooperationConfirmation.objects.create(
        host_organization=host, site=site, municipality="千代田区", submitted_on=TODAY
    )
    site.name = "本社"
    assert not host_records.save_site(site, [category("sewing")], by=None).resubmission_needed
    site.address = "東京都港区芝公園1-1"
    assert host_records.save_site(site, [category("sewing")], by=None).resubmission_needed


# --- 協力確認書（D1）-------------------------------------------------------------------


def test_cooperation_status_shows_the_latest_per_municipality_and_unsubmitted_sites() -> None:
    host = make_host()
    head, second = make_site(host), make_site(host, name="第二工場")
    for municipality, day in (
        ("千代田区", date(2025, 4, 1)),
        ("千代田区", date(2026, 4, 1)),  # 出し直し
        ("港区", date(2025, 5, 1)),  # 住居地（事業所の所在地と一致しなくてよい）
    ):
        host_records.save_cooperation(
            HostCooperationConfirmation(
                host_organization=host, site=head, municipality=municipality, submitted_on=day
            ),
            by=None,
        )
    first, other = hosts.cooperation_status([head, second])
    assert [(c.municipality, c.submitted_on) for c in first.latest] == [
        ("千代田区", date(2026, 4, 1)),
        ("港区", date(2025, 5, 1)),
    ]
    assert other.latest == []  # 未提出


def test_cooperation_site_must_belong_to_the_host() -> None:
    host, other = make_host(), make_host(name="別の会社", corporate_number="7999900000010")
    with pytest.raises(ValidationError) as error:
        host_records.save_cooperation(
            HostCooperationConfirmation(
                host_organization=host,
                site=make_site(other),
                municipality="千代田区",
                submitted_on=TODAY,
            ),
            by=None,
        )
    assert "site" in error.value.error_dict


def test_cooperation_copy_has_no_expiry_task() -> None:
    """協力確認書の控えに有効期限が入っていても、日次の起票は期限のタスクを作らない。"""
    host = make_host()
    confirmation = host_records.save_cooperation(
        HostCooperationConfirmation(
            host_organization=host,
            site=make_site(host),
            municipality="千代田区",
            submitted_on=TODAY,
        ),
        files=[pdf()],
        by=None,
    )
    copy = Attachment.objects.get()
    copy.valid_until = TODAY + timedelta(days=10)
    copy.save()
    filings.create_scheduled(TODAY)
    assert tasks(FilingKind.MEMBERSHIP_RENEWAL, copy) == []
    assert tasks(FilingKind.MEMBERSHIP_RENEWAL, confirmation) == []


def test_migration_cancels_open_cooperation_tasks_with_a_reason() -> None:
    host = make_host()
    confirmation = HostCooperationConfirmation.objects.create(
        host_organization=host, site=make_site(host), municipality="千代田区", submitted_on=TODAY
    )
    copy = Attachment.objects.create(
        kind=AttachmentKind.COOPERATION_CONFIRMATION,
        file=pdf(),
        original_filename="file.pdf",
        size=len(PDF_BYTES),
        sha256="0" * 64,
        valid_until=TODAY + timedelta(days=10),
        target=confirmation,
    )
    open_task, _ = filings.create(
        FilingKind.MEMBERSHIP_RENEWAL,
        target=copy,
        basis_on=TODAY + timedelta(days=10),
        trigger=Trigger.DOCUMENT_EXPIRY,
    )
    submitted, _ = filings.create(
        FilingKind.MEMBERSHIP_RENEWAL,
        target=copy,
        basis_on=TODAY + timedelta(days=40),
        trigger=Trigger.DOCUMENT_EXPIRY,
    )
    submitted.status = FilingStatus.COMPLETED
    submitted.save()

    migration = importlib.import_module(
        "apps.filings.migrations.0007_cancel_cooperation_expiry_tasks"
    )
    migration.cancel_tasks(apps, None)

    open_task.refresh_from_db()
    submitted.refresh_from_db()
    assert open_task.status == FilingStatus.CANCELLED
    assert "有効期限が無い" in open_task.status_reason
    assert submitted.status == FilingStatus.COMPLETED  # 閉じたタスクは変えない
    (entry,) = transition_logs(open_task)
    assert entry.context["automatic"] is True
    assert entry.changes["status"] == ["not_started", "cancelled"]


# --- 加入状況の期限のタスク（D2）--------------------------------------------------------


@pytest.fixture
def membership() -> HostMembership:
    return HostMembership(
        host_organization=make_host(),
        field=field("food_manufacturing"),
        joined_on=date(2025, 4, 1),
        valid_until=TODAY + timedelta(days=20),  # 1 か月前から起票
    )


def test_saving_raises_the_task_and_the_daily_job_does_not_duplicate_it(
    membership: HostMembership,
) -> None:
    result = host_records.save_membership(membership, by=None, today=TODAY)
    (task,) = result.created
    assert task.due_on == membership.valid_until
    assert filings.create_scheduled(TODAY).created == []
    assert tasks(FilingKind.MEMBERSHIP_RENEWAL, membership) == [task]
    # 保存し直しても増えない
    assert host_records.save_membership(membership, by=None, today=TODAY).created == []
    assert len(tasks(FilingKind.MEMBERSHIP_RENEWAL, membership)) == 1


def test_daily_job_first_then_saving_does_not_duplicate(membership: HostMembership) -> None:
    membership.save()  # 画面を通らない登録（admin など）。保存のときの起票はしない
    (task,) = filings.create_scheduled(TODAY).created
    assert host_records.save_membership(membership, by=None, today=TODAY).created == []
    assert tasks(FilingKind.MEMBERSHIP_RENEWAL, membership) == [task]


def test_expiry_before_the_lead_time_raises_nothing(membership: HostMembership) -> None:
    membership.valid_until = TODAY + timedelta(days=200)
    assert host_records.save_membership(membership, by=None, today=TODAY).created == []


def test_replacing_the_certificate_completes_the_old_task_automatically(
    membership: HostMembership,
) -> None:
    user = make_user("tanto", roles=("support_staff",))
    (old_task,) = host_records.save_membership(membership, by=user, today=TODAY).created
    host_records.replace_membership_certificate(
        membership, file=pdf("old.pdf"), valid_until=membership.valid_until, by=user, today=TODAY
    )
    old_file = Attachment.objects.get()
    new_until = TODAY + timedelta(days=365)
    result = host_records.replace_membership_certificate(
        membership, file=pdf("new.pdf"), valid_until=new_until, by=user, today=TODAY
    )
    assert (
        result.closed == [old_task] and result.created == []
    )  # 新しい期限は 1 か月前まで起票しない
    old_task.refresh_from_db()
    membership.refresh_from_db()
    assert membership.valid_until == new_until
    assert old_task.status == FilingStatus.COMPLETED
    assert "証明書の差し替え" in old_task.status_reason
    entry = transition_logs(old_task)[-1]
    assert entry.actor == user
    assert entry.context["automatic"] is True
    assert entry.context["source"] == f"hosts.hostmembership:{membership.pk}"
    # 古い証明書は残る
    assert set(membership.attachments.values_list("pk", flat=True)) >= {old_file.pk}
    assert membership.attachments.count() == 2


def test_human_completion_has_no_automatic_mark(membership: HostMembership) -> None:
    (task,) = host_records.save_membership(membership, by=None, today=TODAY).created
    filings.transition(task, FilingStatus.COMPLETED, by=None)
    (entry,) = transition_logs(task)
    assert "automatic" not in entry.context


@pytest.mark.parametrize("new_until", [TODAY + timedelta(days=10), None])
def test_earlier_or_removed_expiry_cancels_untouched_tasks_and_leaves_started_ones(
    membership: HostMembership, new_until: date | None
) -> None:
    (task,) = host_records.save_membership(membership, by=None, today=TODAY).created
    other = HostMembership.objects.create(
        host_organization=membership.host_organization,
        field=field("industrial_products"),
        joined_on=date(2025, 4, 1),
        valid_until=TODAY + timedelta(days=20),
    )
    (started,) = host_records.save_membership(other, by=None, today=TODAY).created
    filings.transition(started, FilingStatus.IN_PROGRESS, by=None)

    membership.valid_until = new_until
    result = host_records.save_membership(membership, by=None, today=TODAY)
    task.refresh_from_db()
    assert task.status == FilingStatus.CANCELLED and task.status_reason
    assert [f.basis_on for f in result.created] == ([new_until] if new_until else [])

    other.valid_until = new_until
    result = host_records.save_membership(other, by=None, today=TODAY)
    started.refresh_from_db()
    assert started.status == FilingStatus.IN_PROGRESS  # 手が付いているので動かさない
    assert result.left_open == [started]


def test_field_of_a_membership_cannot_be_changed(membership: HostMembership) -> None:
    host_records.save_membership(membership, by=None, today=TODAY)
    membership.field = field("industrial_products")
    with pytest.raises(ValidationError):
        host_records.save_membership(membership, by=None, today=TODAY)


# --- 支援委託契約の期限のタスク（D2・D3）-------------------------------------------------


def test_next_contract_completes_the_renewal_task_of_the_previous_one() -> None:
    host = make_host()
    first_ends_on = TODAY + timedelta(days=60)  # 3 か月前から起票
    first = SupportContract(
        host_organization=host,
        concluded_on=date(2025, 3, 1),
        starts_on=date(2025, 4, 1),
        ends_on=first_ends_on,
    )
    (task,) = host_records.save_support_contract(first, by=None, today=TODAY).created
    assert filings.create_scheduled(TODAY).created == []  # 日次の起票と二重にならない

    renewed = SupportContract(
        host_organization=host,
        concluded_on=TODAY,
        starts_on=first_ends_on + timedelta(days=1),
        ends_on=first_ends_on + timedelta(days=366),
    )
    result = host_records.save_support_contract(renewed, by=None, today=TODAY)
    task.refresh_from_db()
    assert result.closed == [task]
    assert task.status == FilingStatus.COMPLETED
    assert renewed.starts_on.isoformat() in task.status_reason
    assert transition_logs(task)[-1].context["source"] == f"hosts.supportcontract:{renewed.pk}"
    assert filings.create_scheduled(TODAY).created == []  # 更新済みの契約は起票しない


def test_overlapping_contract_is_rejected() -> None:
    host = make_host()
    host_records.save_support_contract(
        SupportContract(
            host_organization=host, concluded_on=TODAY, starts_on=date(2025, 4, 1), ends_on=None
        ),
        by=None,
        today=TODAY,
    )
    with pytest.raises(ValidationError):
        host_records.save_support_contract(
            SupportContract(
                host_organization=host,
                concluded_on=TODAY,
                starts_on=date(2026, 4, 1),
                ends_on=None,
            ),
            by=None,
            today=TODAY,
        )


# --- 添付 ------------------------------------------------------------------------


def test_attachments_use_the_kind_of_the_target_and_are_checked() -> None:
    host = make_host()
    contract = SupportContract.objects.create(
        host_organization=host, concluded_on=TODAY, starts_on=TODAY
    )
    (attachment,) = host_records.add_attachments(contract, [pdf()], by=None)
    assert attachment.kind == AttachmentKind.SUPPORT_CONTRACT
    with pytest.raises(ValidationError):
        host_records.add_attachments(contract, [ContentFile(b"MZ", name="tool.exe")], by=None)
    with pytest.raises(ValidationError):
        host_records.add_attachments(contract, [], by=None)
    assert list(host_records.attachments_of_host(host)) == [attachment]
