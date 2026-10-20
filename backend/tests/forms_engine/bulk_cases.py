"""一括申請のテストの外国人（台帳の項目がそろったもの。T48）。"""

from __future__ import annotations

import datetime
from typing import Any

from apps.contracts.models import EmploymentContract
from apps.core.models import Organization, SupportLanguage
from apps.forms_engine.models import BulkApplication, BulkApplicationEntry
from apps.hosts.models import (
    HostApplicantContact,
    HostDeclaration,
    HostFinancialYear,
)
from apps.workers.models import (
    ApplicationType,
    QualificationArea,
    StatusOfResidence,
    TechnicalInternType,
    WorkerAddress,
    WorkerContact,
    WorkerQualification,
    WorkerStatus,
)
from services import (
    bulk_applications,
    host_declarations,
    immigration_codes,
    staff_periods,
    support_plans,
    workers,
)
from tests.contracts.factories import make_contract
from tests.core.factories import make_staff
from tests.forms_engine.test_form_application import make_case
from tests.workers.factories import add_passport, add_residence, make_worker

BASIS = datetime.date(2026, 10, 20)


def first_code(code: str, procedure: str, startswith: str = "") -> str:
    for value, label in immigration_codes.procedure_table(code, procedure):
        if label.startswith(startswith):
            return value
    raise AssertionError(f"{code} {startswith}")


def complete_host(procedure: str) -> EmploymentContract:
    """自機関・受入れ機関・事業所の一括申請の項目をそろえ、1 人目の雇用契約を返す。"""
    contract = make_case(monthly_working_minutes=10400, hourly_equivalent=1268)
    organization = Organization.objects.get()
    organization.corporate_number = "8010401024011"
    organization.registered_on = datetime.date(2019, 6, 1)
    organization.registration_number = "19登-000123"
    organization.phone_number = "03-0000-1111"
    organization.save()
    host = contract.host_organization
    host.industry_1 = first_code("CC13", procedure, "金属製品製造業")
    host.capital = 30_000_000
    host.phone_number = "03-0000-2222"
    host.save()
    site = contract.versions.get().site
    site.full_time_employees = 50
    site.employment_insurance_number = "12345678901"
    site.labour_insurance_number = "13123456789000"
    site.health_pension_insured = True
    site.labour_insurance_covered = True
    site.save()
    HostFinancialYear.objects.create(
        host_organization=host, fiscal_year_end=datetime.date(2026, 3, 31), sales=300_000_000
    )
    HostDeclaration.objects.create(
        host_organization=host,
        declared_on=datetime.date(2026, 10, 1),
        answers={
            item.key: {
                "answer": item.expected,
                "detail": "労災保険・雇用保険に加入" if item.key == "29" else "",
            }
            for item in host_declarations.items()
            if item.expected
        },
    )
    HostApplicantContact.objects.create(
        host_organization=host, name="申請 担当", relationship="所属機関の職員", is_active=True
    )
    return contract


def complete_worker(contract: EmploymentContract, index: int, procedure: str) -> None:
    """外国人の一括申請の項目（台帳）をそろえる。"""
    host = contract.host_organization
    worker = contract.worker
    worker.name_romaji = f"WANG FANG {chr(65 + index)}"
    worker.birthplace = "中国河南省"
    worker.has_relatives_in_japan = False
    worker.has_work_history = False
    worker.status = WorkerStatus.RESIDING
    worker.save()
    staff = make_staff(name=f"佐藤 花子{index}")
    staff.languages.set(SupportLanguage.objects.filter(code__in=["ja", "zh-hans"]))
    office = staff.office
    office.phone_number = "03-0000-3333"
    office.save()
    manager = make_staff(name=f"山田 一郎{index}", office=office, is_support_manager=True)
    staff_periods.change_office_manager(
        office, manager, starts_on=datetime.date(2026, 4, 1), reason="就任", by=None, today=BASIS
    )
    staff_periods.change_worker_staff(
        worker, staff, starts_on=datetime.date(2026, 4, 1), reason="担当", by=None, today=BASIS
    )
    status = StatusOfResidence.SSW_1
    intern_type = ""
    if procedure == "change":
        status, intern_type = StatusOfResidence.TECHNICAL_INTERN_2, TechnicalInternType.RO
    if procedure != "coe":
        add_residence(
            worker,
            card_number=f"AB1234567{index}CD",
            status_of_residence=status,
            technical_intern_type=intern_type,
            period_of_stay=first_code("CB20", procedure, "１年"),
            permitted_on=datetime.date(2025, 11, 1),
            expires_on=datetime.date(2026, 11, 1),
            application_type=ApplicationType.CHANGE,
        )
    add_passport(worker, number=f"E1234567{index}")
    workers.add_record(
        worker,
        WorkerAddress(
            postal_code="273-0005",
            address="千葉県船橋市本町1-2-3 サンプル寮101",
            effective_from=datetime.date(2026, 4, 1),
        ),
        by=None,
    )
    workers.add_record(
        worker,
        WorkerContact(
            phone="090-1234-5678",
            email="wang@example.com",
            home_country_address="中国河南省鄭州市金水区花園路100号",
        ),
        by=None,
    )
    for area, evaluation, exam in (
        (QualificationArea.SKILL, "03", "004"),
        (QualificationArea.JAPANESE, "03", "002"),
    ):
        WorkerQualification.objects.create(
            worker=worker, area=area, evaluation=evaluation, exam=exam, exam_country="039"
        )
    support_plans.create_plan(worker, host, applies_from=datetime.date(2026, 4, 1), by=None)


def complete_application(procedure: str, people: int = 2) -> BulkApplication:
    first = complete_host(procedure)
    site = first.versions.get().site
    contracts = [first] + [
        make_contract(
            make_worker(name_romaji=f"WORKER {index}"),
            site=site,
            monthly_working_minutes=10400,
            hourly_equivalent=1268,
        )
        for index in range(1, people)
    ]
    for index, contract in enumerate(contracts):
        complete_worker(contract, index, procedure)
    intermediary = make_staff(name="取次 太郎", is_online_application_user=True)
    office = intermediary.office
    office.phone_number = "03-0000-4444"
    office.save()
    application = BulkApplication.objects.create(
        procedure=procedure, basis_on=BASIS, intermediary=intermediary
    )
    bulk_applications.add_workers(application, [c.worker for c in contracts], by=None)
    for entry in application.entries.all():
        fill_inputs(entry, procedure)
    return application


def fill_inputs(entry: BulkApplicationEntry, procedure: str) -> None:
    template = bulk_applications.template_of(entry.application)
    codes = {item["key"]: item.get("code", "") for item in template.mapping.definition["fields"]}
    inputs: dict[str, Any] = dict(entry.inputs) | {
        "spouse": "no",
        "occupation": "会社員",
        "main_occupation": immigration_codes.procedure_table(codes["c.main_occupation"], procedure)[
            0
        ][0],
        "notice_email": "tantou@example.com",
        "receipt": first_code(codes["a.receipt"], procedure, "郵送"),
        "deposit": "no",
        "foreign_fee": "no",
    }
    if procedure == "coe":
        inputs |= {
            "entry_month": "2027年4月",
            "port": first_code("A024", procedure, "成田"),
            "stay_years": "1",
            "stay_months": "0",
            "visa_place": "北京",
            "entry_history": "no",
            "coe_history": "no",
            "deport": "no",
        }
    else:
        inputs |= {
            "desired_period": first_code(
                codes["a.desired_period"],
                procedure,
                "特定技能１号（１年）" if procedure == "extension" else "１年",
            ),
            "reason": "引き続き同じ受入れ機関で就労するため。",
        }
    bulk_applications.save_inputs(
        entry, inputs, sensitive={"crime": "no", "crime_detail": ""}, by=None
    )
