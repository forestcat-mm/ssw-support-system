"""期限ダッシュボード（第5章 F06・F07、T14）。

届出タスクの絞り込み・警告レベル別の件数・対象のリンク先。

- 期限の色は繰り延べ前の期限（unadjusted_due_on）が基準（T10 の assess）。法令上の期限は併記する。
- 無効化した外国人のタスクは出さない（active_workers()）。外国人に紐づかないタスク
  （自機関・受入れ機関）は出す。
- 届出タスク以外の期限（登録更新・加入証明・協力確認書・支援委託契約・旅券・期間満了確認）も、
  T11 で内部タスクとして起票済みなので同じ一覧に出る。
"""

from __future__ import annotations

import datetime
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from django.db.models import Q, QuerySet
from django.urls import reverse

from apps.core.models import Attachment
from apps.filings.models import Filing, FilingKind, FilingStatus
from apps.hosts.models import HostOrganization
from services import deadlines, filings, workers

OPEN_STATUSES: tuple[str, ...] = (FilingStatus.NOT_STARTED, FilingStatus.IN_PROGRESS)
AWAITING_RECEIPT: tuple[str, ...] = (FilingStatus.SUBMITTED,)
COMPLETED: tuple[str, ...] = (
    FilingStatus.ACCEPTED,
    FilingStatus.NOT_REQUIRED,
    FilingStatus.COMPLETED,
)

# 上部に件数を出すレベル（受付前は件数に入れない）
COUNTED_LEVELS = (
    deadlines.Level.OVERDUE,
    deadlines.Level.WITHIN_7,
    deadlines.Level.WITHIN_14,
    deadlines.Level.OPEN,
)


@dataclass(frozen=True)
class FilingRow:
    """届出タスクの 1 行（ダッシュボードと外国人詳細の届出タブで共通）。"""

    filing: Filing
    filer_label: str
    deadline: deadlines.Deadline
    is_open: bool  # 未着手・対応中（警告バッジを出す）
    level: deadlines.Level | None  # 未着手・対応中のときだけ
    target_label: str
    target_url_name: str = ""  # workers:detail / hosts:detail（リンクが無ければ空）
    target_pk: int | None = None
    record_url: str = (
        ""  # 開いている定期面談のタスクから記録の画面へ（T19。表示は権限のある人だけ）
    )
    notes: tuple[str, ...] = ()  # 行に添える注意（services.filings.task_notes）


def _host_id_of(target: object) -> int | None:
    if isinstance(target, HostOrganization):
        return target.pk
    if isinstance(target, Attachment):
        return _host_id_of(target.target)
    host_id = getattr(target, "host_organization_id", None)
    return host_id if isinstance(host_id, int) else None


def _target_link(filing: Filing) -> tuple[str, str, int | None]:
    """対象の表示名とリンク先（外国人の詳細、受入れ機関の詳細、無し）。"""
    if filing.worker_id is not None and filing.worker is not None:
        return str(filing.worker), "workers:detail", filing.worker_id
    host_id = _host_id_of(filing.target)
    if host_id is not None:
        return filing.target_repr, "hosts:detail", host_id
    return filing.target_repr, "", None


def _record_url(filing: Filing) -> str:
    if filing.worker_id is None or filing.worker is None:
        return ""
    if filing.worker.deactivated_at is not None:
        return ""
    if filing.kind == FilingKind.SIGNED_COPY_COLLECTION:
        # 署名済みの書類は帳票タブで添付する（添付すると自動で完了。T33）
        return reverse("workers:documents", args=[filing.worker_id])
    if filing.kind != FilingKind.PERIODIC_INTERVIEW:
        return ""
    url = reverse("workers:support_interview_create", args=[filing.worker_id])
    return f"{url}?filing={filing.pk}"


def filing_rows(items: Iterable[Filing], today: datetime.date) -> list[FilingRow]:
    rows = []
    for filing in items:
        deadline = filings.deadline_of(filing)
        is_open = filing.status in OPEN_STATUSES
        label, url_name, pk = _target_link(filing)
        rows.append(
            FilingRow(
                filing=filing,
                filer_label=str(filings.FILER_LABELS[filings.definition(filing.kind).filer]),
                deadline=deadline,
                is_open=is_open,
                level=deadlines.assess(deadline, today).level if is_open else None,
                target_label=label,
                target_url_name=url_name,
                target_pk=pk,
                record_url=_record_url(filing) if is_open else "",
                notes=tuple(filings.task_notes(filing, today)),
            )
        )
    return rows


@dataclass(frozen=True)
class DashboardQuery:
    assignee_id: int | None = None  # 担当者（届出タスクの担当者で判定）
    unassigned_only: bool = False
    level: deadlines.Level | None = None
    kind: str = ""
    filer: str = ""  # services.filings.Filer の値
    statuses: tuple[str, ...] = OPEN_STATUSES  # 空ならすべて（取消を含む）


@dataclass(frozen=True)
class DashboardResult:
    rows: list[FilingRow]
    level_counts: Counter[deadlines.Level] = field(default_factory=Counter)
    awaiting_receipt: int = 0  # 受理待ち（提出済）の件数。既定の一覧には出さない
    total: int = 0  # 期限のレベルで絞る前の件数（タブ「すべて」の件数）


def base_queryset() -> QuerySet[Filing]:
    """無効化した外国人のタスクを除いた届出タスク。"""
    return Filing.objects.filter(Q(worker__isnull=True) | Q(worker__in=workers.active_workers()))


def dashboard(query: DashboardQuery, today: datetime.date) -> DashboardResult:
    common = base_queryset()
    if query.unassigned_only:
        common = common.filter(assignee__isnull=True)
    elif query.assignee_id is not None:
        common = common.filter(assignee_id=query.assignee_id)
    if query.kind:
        common = common.filter(kind=query.kind)
    if query.filer:
        kinds = [k for k, d in filings.KIND_DEFINITIONS.items() if d.filer == query.filer]
        common = common.filter(kind__in=kinds)

    awaiting = common.filter(status__in=AWAITING_RECEIPT).count()
    listed = common.filter(status__in=query.statuses) if query.statuses else common
    listed = (
        listed.select_related("worker", "assignee", "content_type")
        .prefetch_related("target")
        .order_by("unadjusted_due_on", "pk")
    )
    rows = filing_rows(listed, today)
    counts: Counter[deadlines.Level] = Counter(
        row.level for row in rows if row.level is not None and row.level in COUNTED_LEVELS
    )
    total = len(rows)
    if query.level is not None:
        rows = [row for row in rows if row.level is query.level]
    return DashboardResult(rows=rows, level_counts=counts, awaiting_receipt=awaiting, total=total)


# --- 担当者なし・今後の予定（修正 1 回目 A3）------------------------------------------------

UPCOMING_MONTHS = 6  # 今後の予定は今日から 6 か月先の期限まで（A3-D1）


def unassigned(query: DashboardQuery, today: datetime.date) -> list[FilingRow]:
    """担当者の無い、開いているタスク（受入れ機関の加入・支援委託契約の更新など。A3-D2）。

    担当者で絞った一覧（自分の担当など）には入らないので、別の区分にして全員に出す。全員・未割当で
    絞った一覧には入っているので、そのときは空を返す。
    """
    if query.assignee_id is None:
        return []
    listed = base_queryset().filter(assignee__isnull=True, status__in=OPEN_STATUSES)
    if query.kind:
        listed = listed.filter(kind=query.kind)
    if query.filer:
        kinds = [k for k, d in filings.KIND_DEFINITIONS.items() if d.filer == query.filer]
        listed = listed.filter(kind__in=kinds)
    listed = (
        listed.select_related("worker", "assignee", "content_type")
        .prefetch_related("target")
        .order_by("unadjusted_due_on", "pk")
    )
    return filing_rows(listed, today)


@dataclass(frozen=True)
class UpcomingRow:
    """今後の予定の 1 行（まだタスクになっていない期限、または受付前のタスク）。"""

    kind: str
    kind_label: str
    deadline: deadlines.Deadline
    target_label: str
    target_url_name: str
    target_pk: int | None
    filing: Filing | None  # 受付前のタスク（まだ起票前なら None）
    worker_staff_id: int | None


def _owner(target: object) -> tuple[str, str, int | None, int | None]:
    """対象の表示名・リンク先と、外国人の担当者（外国人に紐づかない期限は None）。"""
    worker = getattr(target, "worker", None)
    if worker is not None:
        return str(worker), "workers:detail", worker.pk, worker.assigned_staff_id
    host_id = _host_id_of(target)
    if host_id is not None:
        host = HostOrganization.objects.filter(pk=host_id).first()
        return (str(host) if host else str(target)), "hosts:detail", host_id, None
    return str(target), "", None, None


def upcoming(query: DashboardQuery, today: datetime.date) -> list[UpcomingRow]:
    """今日から 6 か月先までの期限のうち、まだタスクになっていないもの（受付・着手の日の前）と、
    受付前のタスク。期限の近い順。

    担当者で絞るときは、その担当者の外国人の期限と、外国人に紐づかない期限を出す。
    """
    until = deadlines.add_months(today, UPCOMING_MONTHS)
    rows: list[UpcomingRow] = []
    for candidate in filings.deadline_candidates():
        deadline = filings.definition(candidate.kind).deadline(candidate.basis_on)
        if deadline.opens_on is None or deadline.opens_on <= today:
            continue  # 受付・着手の日を過ぎた期限は日次の起票でタスクになる
        if deadline.unadjusted_due_on > until:
            continue
        label, url_name, pk, staff_id = _owner(candidate.target)
        rows.append(
            UpcomingRow(
                kind=candidate.kind,
                kind_label=str(FilingKind(candidate.kind).label),
                deadline=deadline,
                target_label=label,
                target_url_name=url_name,
                target_pk=pk,
                filing=None,
                worker_staff_id=staff_id,
            )
        )
    for row in filing_rows(
        base_queryset()
        .filter(status__in=OPEN_STATUSES, unadjusted_due_on__lte=until)
        .select_related("worker", "assignee", "content_type")
        .prefetch_related("target"),
        today,
    ):
        if row.level is not deadlines.Level.NOT_YET:
            continue
        worker = row.filing.worker
        rows.append(
            UpcomingRow(
                kind=row.filing.kind,
                kind_label=row.filing.get_kind_display(),
                deadline=row.deadline,
                target_label=row.target_label,
                target_url_name=row.target_url_name,
                target_pk=row.target_pk,
                filing=row.filing,
                worker_staff_id=worker.assigned_staff_id if worker else None,
            )
        )
    if query.assignee_id is not None:
        rows = [r for r in rows if r.worker_staff_id in (None, query.assignee_id)]
    if query.kind:
        rows = [r for r in rows if r.kind == query.kind]
    return sorted(rows, key=lambda r: (r.deadline.unadjusted_due_on, r.kind_label))
