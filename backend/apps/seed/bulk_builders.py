"""seed_dummy の段階4の場面（T55。ADR-0045）。今の場面は変えず、足すだけ。

- 一括申請の台帳の項目（T46a・T46b）: 自機関の法人番号・登録日、事務所の電話、取次者の印
  （佐藤 花子）、受入れ機関の業種・資本金・決算・申告・申請担当者、事業所の常勤職員数・保険、
  受入れ機関の役員（精機工業に「受入れ業務の執行に直接関与しない」役員 1 人）、外国人 1・9・25 番の
  出生地・在日親族・職歴・試験と、1・9 番の在留期間のコード。
- 一括届出の場面: 届出タスクの 7 種のうち、今のダミーに無い種別を手の起票で足す（10〜14 番）。
  雇用契約の変更・終了・受入れ困難・特異事案報告は今のダミーにある。

住所はダミーの市区町村（一括のツールの市区町村の表に無い）のままにする（gate4 で直す。ADR-0045）。
"""

from __future__ import annotations

import datetime

from apps.filings.models import FilingKind, Trigger
from apps.hosts.models import (
    HostApplicantContact,
    HostDeclaration,
    HostFinancialYear,
    HostOfficer,
    HostOrganization,
    SupportContract,
)
from apps.seed.context import DUMMY_MARK, SeedContext
from apps.workers.models import (
    LatestResidenceRecord,
    QualificationArea,
    WorkerQualification,
)
from services import corporate_number, filings, host_declarations, immigration_codes

BULK_WORKERS = (1, 9, 25)  # 更新・変更（9 番の転籍）・認定（25 番。入国前）
INDUSTRIES = {
    "industrial_products": "金属製品製造業",
    "food_manufacturing": "食料品製造業",
}


def _code(code: str, startswith: str) -> str:
    for value, label in immigration_codes.procedure_table(code, "extension"):
        if label.startswith(startswith):
            return value
    return ""


def _hosts(ctx: SeedContext) -> list[HostOrganization]:
    hosts = list(ctx.hosts)
    for site in ctx.extra_sites:
        if site.host_organization not in hosts:
            hosts.append(site.host_organization)
    return hosts


def seed_ledger_items(ctx: SeedContext) -> None:
    organization = ctx.organization
    if organization is not None and organization.registered_on is None:
        organization.corporate_number = organization.corporate_number or (
            corporate_number.generate_dummy(ctx.rng)
        )
        organization.registered_on = ctx.days(-1500)
        organization.save()
    office = ctx.staff[0].office
    if not office.phone_number:
        office.phone_number = "000-0000-9001"
        office.save()
    intermediary = ctx.staff[1]
    intermediary.is_online_application_user = True
    intermediary.online_application_email = "dummy_tanto@example.invalid"
    intermediary.save()
    expected = {
        item.key: {
            "answer": item.expected,
            "detail": "労災保険・雇用保険に加入（ダミー）" if item.key == "29" else "",
        }
        for item in host_declarations.items()
        if item.expected
    }
    for number, host in enumerate(_hosts(ctx), start=1):
        site = host.sites.first()
        field = site.field.code if site is not None else ""
        host.industry_1 = _code("CC13", INDUSTRIES.get(field, "金属製品製造業"))
        host.capital = 30_000_000 * number
        host.save()
        for year in (1, 2, 3):
            ctx.keep(
                HostFinancialYear.objects.create(
                    host_organization=host,
                    fiscal_year_end=datetime.date(ctx.base.year - year, 3, 31),
                    sales=300_000_000 - year * 10_000_000,
                    ordinary_profit=10_000_000,
                    net_profit=5_000_000,
                    net_assets=80_000_000,
                )
            )
        ctx.keep(
            HostDeclaration.objects.create(
                host_organization=host,
                declared_on=ctx.days(-30),
                answers=expected,
                left_japanese_voluntary=1,
                left_japanese_involuntary=0,
                left_foreign_voluntary=0,
                left_foreign_involuntary=0,
                missing_ssw=0,
                missing_interns=0,
            )
        )
        ctx.keep(
            HostApplicantContact.objects.create(
                host_organization=host,
                name=f"申請 担当{DUMMY_MARK}",
                relationship="所属機関の職員",
                is_active=True,
            )
        )
        for site in host.sites.all():
            site.full_time_employees = 40 + number
            site.employment_insurance_number = f"1234567890{number}"
            site.labour_insurance_number = f"1312345678900{number}"
            site.health_pension_insured = True
            site.labour_insurance_covered = True
            site.save()
    first = ctx.hosts[0]
    for name, kana, position, involved in (
        (f"代表 太郎{DUMMY_MARK}", "だいひょう たろう", "代表取締役", True),
        (f"監査 花子{DUMMY_MARK}", "かんさ はなこ", "監査役", False),
    ):
        ctx.keep(
            HostOfficer.objects.create(
                host_organization=first,
                name=name,
                name_kana=kana,
                position=position,
                appointed_on=ctx.days(-1000),
                not_involved_in_acceptance=not involved,
            )
        )
    for number in BULK_WORKERS:
        worker = ctx.worker(number)
        worker.birthplace = "中国河南省（ダミー）"
        worker.has_relatives_in_japan = False
        worker.has_work_history = False
        worker.save()
        for area, exam in ((QualificationArea.SKILL, "004"), (QualificationArea.JAPANESE, "002")):
            ctx.keep(
                WorkerQualification.objects.create(
                    worker=worker, area=area, evaluation="03", exam=exam, exam_country="039"
                )
            )
        latest = (
            LatestResidenceRecord.objects.select_related("record").filter(worker=worker).first()
        )
        if latest is not None and number != 25 and not latest.record.period_of_stay:
            record = latest.record
            record.period_of_stay = _code("CB20", "１年")
            record.save()


def seed_efile_filings(ctx: SeedContext) -> None:
    """一括届出の 7 種のうち、今のダミーに無い種別の届出タスク（手の起票。10〜14 番）。"""
    on = ctx.days(-5)
    support = SupportContract.objects.filter(host_organization=ctx.hosts[0]).first()
    plans: list[tuple[int, FilingKind, dict[str, object]]] = [
        (
            10,
            FilingKind.SUPPORT_PLAN_CHANGE,
            {"plan_change_item": "III-6", "event": "concluded"},
        ),
        (11, FilingKind.SUPPORT_CONTRACT_CHANGE, {"changes": {"monthly_fee": [30000, 32000]}}),
        (12, FilingKind.MISSING, {}),
        (13, FilingKind.STANDARD_NONCONFORMITY, {}),
        (14, FilingKind.SUPPORT_PLAN_DIFFICULTY, {}),
    ]
    for number, kind, details in plans:
        if support is not None and kind is FilingKind.SUPPORT_CONTRACT_CHANGE:
            details = {**details, "support_contract": support.pk, "event": "changed"}
        filings.create(
            kind,
            target=ctx.worker(number),
            basis_on=on,
            trigger=Trigger.MANUAL,
            details=details,
        )
