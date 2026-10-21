"""外国人の詳細の上の帯と概要のタブ（U3。仕様 5.2）。表示のための集め方だけで、判定は変えない。

- 上の帯: 在留期限（期限のレベル）・在留資格・受入れ機関・分野・担当・支援言語。
- 今やること（U3-D3）: 未完了の届出タスク（期限の近い順に 5 件まで）と、この人を入れた
  作りかけの一括申請（Excel をまだ作っていないもの）・提出用の PDF の下書きの不足の件数。
- 概要の 5 雇用契約・6 支援の要約。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from django.db.models import Q
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.contracts.models import EmploymentContract, EmploymentContractVersion
from apps.core.templatetags.ui import level_text  # 期限の文字は画面の札と同じにする
from apps.filings.models import FilingKind
from apps.forms_engine.models import BulkApplicationEntry, SubmissionDraft
from apps.hosts.models import SupportContract
from apps.support.models import (
    OPEN_CONSULTATION_STATUSES,
    Consultation,
    SupportPlanVersion,
    SupportRecord,
)
from apps.workers.models import ForeignWorker
from services import (
    application_sets,
    bulk_applications,
    contracts,
    deadlines,
    filings,
    worker_detail,
)
from services.support_catalog import SupportItem

TODO_LIMIT = 5
OPEN_FILING_STATUSES = tuple(status.value for status in filings.OPEN_STATUSES)

# 期限のレベル → 点と面の色（仕様 1: danger・caution・ok・neutral）
TONES = {
    deadlines.Level.OVERDUE: "danger",
    deadlines.Level.WITHIN_7: "caution",
    deadlines.Level.WITHIN_14: "ok",
    deadlines.Level.OPEN: "neutral",
    deadlines.Level.NOT_YET: "neutral",
}

# 届出タスクの種別 → 概要のどのセクションに関係するか（今やることの「関係するセクション」）
SECTION_OF_KIND = {
    FilingKind.RESIDENCE_RENEWAL: "residence",
    FilingKind.PASSPORT_CHECK: "passport",
    FilingKind.CONTRACT_CHANGE: "contract",
    FilingKind.CONTRACT_TERMINATION: "contract",
    FilingKind.CONTRACT_NEW: "contract",
    FilingKind.CONTRACT_EXPIRY_CHECK: "contract",
    FilingKind.SIGNED_COPY_COLLECTION: "contract",
    FilingKind.PERIODIC_INTERVIEW: "support",
    FilingKind.SUPPORT_PLAN_CHANGE: "support",
    FilingKind.SUPPORT_PLAN_DIFFICULTY: "support",
    FilingKind.SUPPORT_DIFFICULTY: "support",
}


@dataclass(frozen=True)
class Expiry:
    deadline: deadlines.Deadline
    level: deadlines.Level
    tone: str
    days_left: int


@dataclass(frozen=True)
class Header:
    residence_status: str  # 在留資格（最新の在留情報。無ければ空）
    expiry: Expiry | None
    contract: EmploymentContract | None
    field_name: str


@dataclass(frozen=True)
class TodoItem:
    tone: str  # danger・caution・ok・neutral
    state: str  # 「超過 5日」「不足 4」など
    title: str
    note: str
    section: str  # 概要のセクションの id（無ければ空）
    action: str
    url: str


@dataclass(frozen=True)
class ContractSummary:
    contract: EmploymentContract
    version: EmploymentContractVersion
    site_name: str
    field_and_categories: str
    support_contract: SupportContract | None


@dataclass(frozen=True)
class SupportSummary:
    plan_version: SupportPlanVersion | None
    next_interview: deadlines.Deadline | None
    next_interview_level: deadlines.Level | None
    last_interview_on: datetime.date | None
    open_consultations: int


@dataclass
class Overview:
    todos: list[TodoItem] = field(default_factory=list)
    contract: ContractSummary | None = None
    support: SupportSummary | None = None
    warnings: set[str] = field(default_factory=set)  # 注意のあるセクション（目次の点）
    allowed: Allowed | None = None


def expiry_of(info: worker_detail.BasicInfo, today: datetime.date) -> Expiry | None:
    if info.residence_deadline is None:
        return None
    assessment = deadlines.assess(info.residence_deadline, today)
    return Expiry(
        deadline=info.residence_deadline,
        level=assessment.level,
        tone=TONES[assessment.level],
        days_left=assessment.days_left,
    )


def header(worker: ForeignWorker, info: worker_detail.BasicInfo, today: datetime.date) -> Header:
    contract = worker_detail.default_contract(worker_detail.contracts_of(worker), today)
    version = contracts.version_on(contract, today) if contract is not None else None
    site = version.site if version is not None else None
    return Header(
        residence_status=(
            info.residence.get_status_of_residence_display() if info.residence else ""
        ),
        expiry=expiry_of(info, today),
        contract=contract,
        field_name=site.field.name if site is not None and site.field_id else "",
    )


def _filing_todos(worker: ForeignWorker, today: datetime.date) -> list[TodoItem]:
    items = []
    for row in worker_detail.worker_filings(worker):
        if not row.is_open or row.level is None:
            continue
        assessment = deadlines.assess(row.deadline, today)
        items.append(
            TodoItem(
                tone=TONES[assessment.level],
                state=level_text(assessment, row.deadline.unadjusted_due_on),
                title=row.filing.get_kind_display(),
                note=_("%(filer)s · due %(day)s")
                % {"filer": row.filer_label, "day": row.deadline.unadjusted_due_on.isoformat()},
                section=SECTION_OF_KIND.get(FilingKind(row.filing.kind), ""),
                action=_("Record") if row.record_url else _("Open"),
                url=row.record_url or reverse("filings:detail", args=[row.filing.pk]),
            )
        )
    return items[:TODO_LIMIT]


def _bulk_todos(worker: ForeignWorker) -> list[TodoItem]:
    """この人を入れた、まだ Excel を作っていない一括申請の不足の件数。"""
    items = []
    entries = (
        BulkApplicationEntry.objects.filter(worker=worker, application__files__isnull=True)
        .select_related("application")
        .order_by("application__basis_on", "pk")
        .distinct()
    )
    for entry in entries:
        application = entry.application
        problems = [p for p in bulk_applications.check(application) if p.entry.pk == entry.pk]
        items.append(
            TodoItem(
                tone="caution" if problems else "neutral",
                state=_("Missing %(count)s") % {"count": len(problems)},
                title=_("Bulk application (%(procedure)s)")
                % {"procedure": application.get_procedure_display()},
                note=_("Basis date %(day)s · not made into Excel yet")
                % {"day": application.basis_on.isoformat()},
                section="",
                action=_("Open"),
                url=reverse("forms:bulk", args=[application.pk]),
            )
        )
    return items


def _draft_todos(worker: ForeignWorker, today: datetime.date) -> list[TodoItem]:
    """提出用の PDF の下書き（この人・手続ごと）の、申請の書類の一覧の不足の件数。"""
    items = []
    for draft in SubmissionDraft.objects.filter(worker=worker).order_by("pk"):
        found = application_sets.overview(worker, draft.set_id, today)
        missing = found.open_count
        if not missing:
            continue
        query = f"?set_id={draft.set_id}&basis_on={today.isoformat()}"
        items.append(
            TodoItem(
                tone="caution",
                state=_("Missing %(count)s") % {"count": missing},
                title=_("Application documents (%(title)s)") % {"title": found.definition.name},
                note=_("A draft of the PDF for submission exists"),
                section="contract",
                action=_("Open the list"),
                url=reverse("workers:application_set", args=[worker.pk]) + query,
            )
        )
    return items


def contract_summary(
    contract: EmploymentContract | None, today: datetime.date
) -> ContractSummary | None:
    if contract is None:
        return None
    version = contracts.version_on(contract, today)
    if version is None:
        return None
    site = version.site
    categories = "・".join(str(category) for category in version.categories.all())
    field_name = site.field.name if site.field_id else ""
    support_contract = (
        SupportContract.objects.filter(
            host_organization=contract.host_organization, starts_on__lte=today
        )
        .filter(Q(ends_on__isnull=True) | Q(ends_on__gte=today))
        .filter(Q(terminated_on__isnull=True) | Q(terminated_on__gte=today))
        .order_by("-starts_on")
        .first()
    )
    return ContractSummary(
        contract=contract,
        version=version,
        site_name=site.name,
        field_and_categories="・".join(part for part in (field_name, categories) if part),
        support_contract=support_contract,
    )


def support_summary(worker: ForeignWorker, today: datetime.date) -> SupportSummary:
    plan_version = (
        SupportPlanVersion.objects.filter(plan__worker=worker, applies_from__lte=today)
        .order_by("-applies_from", "-version")
        .first()
    )
    interview = (
        worker.filings.filter(kind=FilingKind.PERIODIC_INTERVIEW, status__in=OPEN_FILING_STATUSES)
        .order_by("unadjusted_due_on")
        .first()
    )
    next_deadline = filings.deadline_of(interview) if interview is not None else None
    last = (
        SupportRecord.objects.filter(
            worker=worker, kind=SupportItem.PERIODIC_INTERVIEW, voided_at__isnull=True
        )
        .order_by("-occurred_on")
        .values_list("occurred_on", flat=True)
        .first()
    )
    return SupportSummary(
        plan_version=plan_version,
        next_interview=next_deadline,
        next_interview_level=(
            deadlines.assess(next_deadline, today).level if next_deadline else None
        ),
        last_interview_on=last,
        open_consultations=Consultation.objects.filter(
            worker=worker, status__in=OPEN_CONSULTATION_STATUSES
        ).count(),
    )


@dataclass(frozen=True)
class Allowed:
    """見る人の権限（ないものは概要に出さない。タブの権限と同じ）。"""

    contracts: bool
    support: bool
    filings: bool
    forms: bool


def overview(
    worker: ForeignWorker,
    head: Header,
    info: worker_detail.BasicInfo,
    today: datetime.date,
    allowed: Allowed,
) -> Overview:
    todos = [
        *(_filing_todos(worker, today) if allowed.filings else []),
        *(_bulk_todos(worker) if allowed.forms else []),
        *(_draft_todos(worker, today) if allowed.forms else []),
    ]
    result = Overview(
        todos=todos,
        contract=contract_summary(head.contract, today) if allowed.contracts else None,
        support=support_summary(worker, today) if allowed.support else None,
        allowed=allowed,
    )
    if any(item.tone == "danger" for item in todos):
        result.warnings.add("todo")
    if head.expiry is not None and head.expiry.level is deadlines.Level.OVERDUE:
        result.warnings.add("residence")
    if (
        result.support is not None
        and result.support.next_interview_level is deadlines.Level.OVERDUE
    ):
        result.warnings.add("support")
    return result


def initial_of(worker: ForeignWorker) -> str:
    """写真が無いときの丸の頭文字（仕様 6）。

    漢字の氏名の最初の字、無ければローマ字の氏名の最初の語の頭文字（旅券のローマ字は苗字が先）。
    """
    if worker.name_kanji.strip():
        return worker.name_kanji.strip()[0]
    words = worker.name_romaji.split()
    return words[0][0].upper() if words else "?"
