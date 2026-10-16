"""雇用契約のテスト用ヘルパ。"""

from datetime import date, time
from typing import Any

from apps.contracts.models import (
    EmploymentContract,
    EmploymentContractAllowance,
    EmploymentContractDeduction,
    EmploymentContractVersion,
)
from apps.hosts.models import HostSite
from apps.workers.models import ForeignWorker
from services import contracts
from tests.hosts.factories import category, make_host, make_site
from tests.workers.factories import make_worker


def version_values(site: HostSite, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "changed_on": date(2026, 4, 1),
        "starts_on": date(2026, 4, 1),
        "ends_on": date(2027, 3, 31),
        "renewal": "possible",
        "site": site,
        "duties": "機械金属加工の作業",
        "start_time": time(8, 30),
        "end_time": time(17, 30),
        "daily_working_minutes": 480,
        "break_minutes": 60,
        "weekly_working_minutes": 2400,
        "weekly_working_days": 5,
        "annual_holidays": 120,
        "paid_leave_days": 10,
        "wage_type": "monthly",
        "base_wage": 220000,
        "wage_closing_day": "毎月末日",
        "wage_payment_day": "翌月25日",
        "payment_method": "bank_transfer",
    }
    return values | overrides


def site_with_categories() -> HostSite:
    site = make_site(make_host())
    site.categories.set([category("machine_metal_processing"), category("sewing")])
    return site


def make_contract(
    worker: ForeignWorker | None = None, site: HostSite | None = None, **overrides: Any
) -> EmploymentContract:
    site = site or site_with_categories()
    version = EmploymentContractVersion(**version_values(site, **overrides))
    contracts.add_version(
        version,
        contract=None,
        worker=worker or make_worker(),
        host_organization=site.host_organization,
        by=None,
    )
    version.categories.set([category("machine_metal_processing")])
    EmploymentContractAllowance.objects.create(version=version, name="住宅手当", amount=10000)
    EmploymentContractDeduction.objects.create(version=version, kind="housing", amount=30000)
    return version.contract


def add_version(contract: EmploymentContract, **overrides: Any) -> EmploymentContractVersion:
    latest = contracts.latest_version(contract)
    assert latest is not None
    values = version_values(latest.site, change_reason="変更", **overrides)
    version = EmploymentContractVersion(**values)
    contracts.add_version(version, contract=contract, by=None)
    version.categories.set(latest.categories.all())
    return version
