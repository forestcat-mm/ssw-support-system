"""seed_dummy（ダミーデータ）。件数・再現性・期限の分布・起票・安全装置・リセット。"""

from collections import Counter
from datetime import date, timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from pytest_django.fixtures import Settings

from apps.contracts.models import EmploymentContract
from apps.filings.models import Filing, FilingKind
from apps.hosts.models import HostOrganization, HostSite
from apps.seed import builders
from apps.seed.models import SeedObject
from apps.workers.models import (
    ForeignWorker,
    LatestResidenceRecord,
    PassportRecord,
    ResidenceRecord,
)
from services import corporate_number, passport, residence_card
from tests.core.factories import make_organization
from tests.hosts.factories import make_host
from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db

BASE = date(2026, 10, 16)


@pytest.fixture(autouse=True)
def _allow(settings: Settings) -> None:
    settings.DEBUG = True
    settings.ALLOW_DUMMY_DATA = True


def _seed(*args: str) -> str:
    out = StringIO()
    call_command("seed_dummy", "--date", BASE.isoformat(), *args, stdout=out)
    return out.getvalue()


def _fingerprint() -> list[tuple[str, str, str]]:
    return sorted(
        (
            w.name_romaji,
            w.name_kanji,
            w.latest_residence.record.card_number if hasattr(w, "latest_residence") else "",
        )
        for w in ForeignWorker.objects.all()
    )


def test_counts_and_running_twice_does_not_add_rows() -> None:
    """T12 の完了条件: 何度実行しても同じ件数で再現。"""
    _seed()
    assert ForeignWorker.objects.count() == 30
    assert Counter(ForeignWorker.objects.values_list("nationality__code", flat=True)) == {
        "CN": 25,
        "VN": 3,
        "ID": 2,
    }
    assert HostOrganization.objects.count() == 2
    assert HostSite.objects.count() == 3
    assert EmploymentContract.objects.count() == 30
    rows = SeedObject.objects.count()
    assert "already exists" in _seed()
    assert SeedObject.objects.count() == rows
    assert ForeignWorker.objects.count() == 30


def test_reset_recreates_the_same_content_for_the_same_base_date() -> None:
    _seed()
    first = _fingerprint()
    filings_before = sorted(Filing.objects.values_list("kind", "basis_on", "due_on"))
    _seed("--reset")
    assert _fingerprint() == first
    assert sorted(Filing.objects.values_list("kind", "basis_on", "due_on")) == filings_before


def test_residence_expiry_distribution() -> None:
    _seed()
    expiries = [
        (latest.record.expires_on - BASE).days
        for latest in LatestResidenceRecord.objects.select_related("record", "worker").exclude(
            worker__status="pre_entry"
        )
    ]
    assert len(expiries) == 29
    assert sum(1 for d in expiries if d < 0) == 1
    assert sum(1 for d in expiries if 0 <= d <= 92) == 5
    assert sum(1 for d in expiries if 92 < d <= 183) == 6
    assert sum(1 for d in expiries if d >= 365) == 17
    histories = Counter(ResidenceRecord.objects.values_list("worker_id", flat=True))
    assert sum(1 for count in histories.values() if count >= 2) == 6
    soon = [p for p in PassportRecord.objects.all() if p.expires_on <= BASE + timedelta(days=183)]
    assert len(soon) == 3


def test_numbers_pass_the_format_checks() -> None:
    _seed()
    cards = [r.card_number for r in ResidenceRecord.objects.all() if r.card_number]
    assert cards and all(residence_card.is_valid(c) for c in cards)
    for record in PassportRecord.objects.all():
        passport.validate(record.number)
    for host in HostOrganization.objects.all():
        assert corporate_number.is_valid(host.corporate_number)
        assert host.corporate_number[1:3] == "99"
        assert "（ダミー）" in host.name


def test_filings_are_created_for_dashboard() -> None:
    _seed()
    kinds = Counter(Filing.objects.values_list("kind", flat=True))
    assert kinds[FilingKind.CONTRACT_TERMINATION] == 2
    assert kinds[FilingKind.ACCEPTANCE_DIFFICULTY] == 1  # 会社都合だけ
    assert kinds[FilingKind.CONTRACT_CHANGE] == 2  # 減給の 2 人（昇給だけの 3 人は起票しない）
    # 在留期限まで 3 か月以内の 6 人のうち、契約を終了した 2 人は「離職」なので在留中の 4 人だけ
    assert kinds[FilingKind.RESIDENCE_RENEWAL] == 4
    assert kinds[FilingKind.PASSPORT_CHECK] == 3
    assert kinds[FilingKind.REGISTRATION_RENEWAL] == 1
    assert kinds[FilingKind.MEMBERSHIP_RENEWAL] == 1
    assert kinds[FilingKind.SUPPORT_CONTRACT_RENEWAL] == 1
    assert kinds[FilingKind.CONTRACT_EXPIRY_CHECK] == 1


def test_staff_and_login_user() -> None:
    _seed()
    from apps.core.models import Staff, User

    assert Staff.objects.count() == 3
    user = User.objects.get(username=builders.DUMMY_USERNAME)
    assert not user.has_usable_password()
    assert Staff.objects.get(user=user)
    assert set(ForeignWorker.objects.values_list("assigned_staff__name", flat=True)) == {
        "佐藤 花子（ダミー）",
        "鈴木 次郎（ダミー）",
    }


@pytest.mark.parametrize(("debug", "allowed"), [(False, True), (True, False)])
def test_refuses_outside_development(settings: Settings, debug: bool, allowed: bool) -> None:
    settings.DEBUG = debug
    settings.ALLOW_DUMMY_DATA = allowed
    with pytest.raises(CommandError):
        _seed()
    with pytest.raises(CommandError):
        _seed("--reset")
    assert not ForeignWorker.objects.exists()


def test_reset_keeps_non_dummy_data() -> None:
    organization = make_organization()
    own_worker = make_worker(name_romaji="REAL TEST")
    own_host = make_host(name="手入力の会社", corporate_number="7000012050002")
    _seed()
    assert ForeignWorker.objects.count() == 31
    _seed("--reset")
    assert ForeignWorker.objects.filter(pk=own_worker.pk).exists()
    assert HostOrganization.objects.filter(pk=own_host.pk).exists()
    assert ForeignWorker.objects.count() == 31
    from apps.core.models import Organization

    assert list(Organization.objects.all()) == [organization]  # 既存の自機関は触らない
