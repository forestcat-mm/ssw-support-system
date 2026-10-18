"""第4-3号（特異事案報告）の届出タスクの自動の起票（T24a、ADR-0012）。

- 起票のきっかけ: 定期面談の通報（T19。通報が「要」）と ⑥ 基準不適合等が「有り」（T35-D5。同じ組の
  きっかけは 1 件にまとめる）、相談の経過の通報・ハローワーク相談（T20）、非自発的離職時の転職支援
  （T18。離職の区分が「非自発的」のときだけ）。
- 事由発生日: 通報日・相談日が入っていればその日、無ければ記録の日。期限は 14 日
  （随時届出と同じ数え方。ADR-0008）。
- 同じ外国人・同じ事由発生日の未完了のタスクがあれば、それにまとめて trigger_details["sources"] に
  元の記録の参照を並べる。同じ日のタスクが提出済みなどで閉じていれば、新しいタスクを起票する
  （報告済みの事由と混ぜない）。
- 元の記録の取消: 参照だけを外して監査ログに残す。参照が無くなったら、未着手のタスクは取消にし、
  手が付いていれば動かさずに人に知らせる（T19 と同じ）。
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils.translation import gettext as _

from apps.core.models import AuditAction, User
from apps.filings.models import Filing, FilingKind, FilingStatus, Trigger
from apps.support.models import ConsultationEntry, PeriodicInterview, SupportRecord
from apps.workers.models import ForeignWorker
from services import audit, filings
from services.support_catalog import SupportItem

KIND = FilingKind.SUPPORT_DIFFICULTY  # 第4-3号（登録支援機関の特異事案報告）
INVOLUNTARY = "involuntary"  # 転職支援の記録の離職の区分（details["separation"]）


# --- 元の記録の参照（T24 の候補の source_key と同じ形） ------------------------------------


def interview_key(interview: PeriodicInterview) -> str:
    return f"periodic_interview:{interview.pk}"


def entry_report_key(entry: ConsultationEntry) -> str:
    return f"consultation_entry:{entry.pk}:report"


def entry_hello_work_key(entry: ConsultationEntry) -> str:
    return f"consultation_entry:{entry.pk}:hello_work"


def record_key(record: SupportRecord) -> str:
    return f"support_record:{record.pk}"


@dataclass(frozen=True)
class Incident:
    worker: ForeignWorker
    occurred_on: datetime.date
    source_key: str


def nonconformity_key(interview: PeriodicInterview) -> str:
    """定期面談の「⑥ 基準不適合等の有無」が「有り」（T35-D5）。"""
    return f"periodic_interview:{interview.pk}:nonconformity"


def interview_keys(interview: PeriodicInterview) -> list[str]:
    return [interview_key(interview), nonconformity_key(interview)]


def of_interview(interview: PeriodicInterview) -> list[Incident]:
    """通報が「要」（T19）と、⑥ 基準不適合等が「有り」（T35-D5）。

    同じ組の 2 つのきっかけは同じ事由として 1 件にまとめる（同じ日にそろえ、参照を両方残す）:
    通報が「要」なら通報日（無ければ面談の日）、⑥ だけなら面談の日（把握した日）。
    """
    if interview.voided_at is not None:
        return []
    on = (interview.reported_on or interview.basis_on) if interview.report_required else None
    result = []
    if interview.report_required:
        result.append(
            Incident(interview.worker, on or interview.basis_on, interview_key(interview))
        )
    nonconformity = getattr(interview, "nonconformity", None)
    if nonconformity is not None and nonconformity.exists:
        result.append(
            Incident(interview.worker, on or interview.basis_on, nonconformity_key(interview))
        )
    return result


def of_entry(entry: ConsultationEntry) -> list[Incident]:
    worker = entry.consultation.worker
    result = []
    if entry.reported:
        result.append(
            Incident(worker, entry.reported_on or entry.occurred_on, entry_report_key(entry))
        )
    if entry.hello_work:
        result.append(
            Incident(worker, entry.hello_work_on or entry.occurred_on, entry_hello_work_key(entry))
        )
    return result


def of_record(record: SupportRecord) -> list[Incident]:
    """非自発的離職時の転職支援だけ（離職の区分が空の既存の記録・自発的離職は起票しない）。"""
    if record.kind != SupportItem.JOB_CHANGE or record.voided_at is not None:
        return []
    if record.details.get("separation") != INVOLUNTARY:
        return []
    return [Incident(record.worker, record.occurred_on, record_key(record))]


# --- 起票 ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Raised:
    filing: Filing
    outcome: str  # "created"（新しく起票）/ "added"（同じ日の未完了のタスクに追加）


def _sources(filing: Filing) -> list[str]:
    return list(filing.trigger_details.get("sources", []))


def raise_tasks(incidents: Iterable[Incident], *, by: User | None) -> list[Raised]:
    """事由ごとに第4-3号のタスクを起票するか、同じ外国人・同じ日の未完了のタスクに参照を足す。"""
    grouped: dict[tuple[int, datetime.date], list[Incident]] = defaultdict(list)
    workers: dict[int, ForeignWorker] = {}
    for incident in incidents:
        grouped[(incident.worker.pk, incident.occurred_on)].append(incident)
        workers[incident.worker.pk] = incident.worker
    result = []
    with transaction.atomic():
        for (worker_id, on), items in grouped.items():
            worker = workers[worker_id]
            keys = [item.source_key for item in items]
            existing = filings.find_unfinished(KIND, worker, on)
            if existing is None:
                filing, created = filings.create_if_active(
                    KIND,
                    target=worker,
                    basis_on=on,
                    trigger=Trigger.INCIDENT_RECORDED,
                    by=by,
                    details={"sources": keys},
                    reuse_closed=False,  # 閉じたタスクがあっても新しく起票する
                )
                if filing is not None and created:
                    result.append(Raised(filing, "created"))
                continue
            current = _sources(existing)
            added = [key for key in keys if key not in current]
            if not added:
                continue
            with audit.event(
                AuditAction.UPDATE, target=existing, actor=by, context={"added_sources": added}
            ):
                existing.trigger_details = {**existing.trigger_details, "sources": current + added}
                existing.updated_by = by
                existing.save(update_fields=["trigger_details", "updated_by", "updated_at"])
            result.append(Raised(existing, "added"))
    return result


# --- 取消での巻き戻し ---------------------------------------------------------------------


def _referring(keys: Iterable[str]) -> QuerySet[Filing]:
    """参照（sources）にどれかを含む第4-3号のタスク（取消を除く）。"""
    condition = Q(pk__in=[])
    for key in keys:
        condition |= Q(trigger_details__sources__contains=[key])
    return Filing.objects.filter(condition, kind=KIND).exclude(status=FilingStatus.CANCELLED)


@dataclass
class Withdrawn:
    cancelled: list[Filing] = field(
        default_factory=list
    )  # 参照が無くなり、未着手だったので取消にした
    kept: list[Filing] = field(default_factory=list)  # ほかの参照が残っているのでそのまま
    needs_attention: list[Filing] = field(default_factory=list)  # 参照が無くなったが手が付いている


def withdraw(source_keys: Iterable[str], *, by: User | None, reason: str) -> Withdrawn:
    """取り消した記録の参照をタスクから外す（監査ログに残す）。参照が無くなったタスクだけを巻き戻す。"""
    keys = set(source_keys)
    result = Withdrawn()
    if not keys:
        return result
    with transaction.atomic():
        for filing in _referring(keys).select_for_update().order_by("pk"):
            sources = _sources(filing)
            removed = [key for key in sources if key in keys]
            if not removed:
                continue
            with audit.event(
                AuditAction.UPDATE,
                target=filing,
                actor=by,
                context={"removed_sources": removed, "reason": reason},
            ):
                filing.trigger_details = {
                    **filing.trigger_details,
                    "sources": [key for key in sources if key not in keys],
                }
                filing.updated_by = by
                filing.save(update_fields=["trigger_details", "updated_by", "updated_at"])
            if _sources(filing):
                result.kept.append(filing)
            elif filing.status == FilingStatus.NOT_STARTED:
                note = _("The record that raised this report task was voided: %(reason)s") % {
                    "reason": reason
                }
                filings.transition(filing, FilingStatus.CANCELLED, by=by, reason=note)
                result.cancelled.append(filing)
            else:
                result.needs_attention.append(filing)
    return result


def tasks_for(source_keys: Iterable[str]) -> list[Filing]:
    """元の記録から起票した（参照を持つ）第4-3号のタスク（取消を除く）。画面の通知に使う。"""
    return list(_referring(set(source_keys)).order_by("pk"))


# --- 第4-3号のタスクから元の面談と報告書へ（T35-D6）------------------------------------------


@dataclass(frozen=True)
class InterviewSource:
    interview: PeriodicInterview
    reasons: tuple[str, ...]  # "report"（通報が要）・"nonconformity"（⑥ が有り）
    reports: tuple[Any, ...]  # その組の最新の第5-5号・第5-6号（完了したもの）


def interview_sources(filing: Filing) -> list[InterviewSource]:
    """タスクの参照（sources）のうち、定期面談の組と、その組の最新の報告書（手で紐づける）。"""
    from apps.forms_engine.models import GeneratedDocument, GenerationStatus  # 循環を避ける

    reasons: dict[int, list[str]] = defaultdict(list)
    for key in _sources(filing):
        parts = key.split(":")
        if parts[0] == "periodic_interview" and len(parts) >= 2 and parts[1].isdigit():
            reasons[int(parts[1])].append(
                "nonconformity" if parts[2:] == ["nonconformity"] else "report"
            )
    if not reasons:
        return []
    content_type = ContentType.objects.get_for_model(PeriodicInterview)
    result = []
    for interview in PeriodicInterview.objects.filter(pk__in=reasons).order_by("basis_on", "pk"):
        latest = []
        for form_id in ("5-5", "5-6"):
            document = (
                GeneratedDocument.objects.filter(
                    content_type=content_type,
                    object_id=interview.pk,
                    template__form_id=form_id,
                    status=GenerationStatus.COMPLETED,
                )
                .select_related("template")
                .order_by("-generated_at", "-pk")
                .first()
            )
            if document is not None:
                latest.append(document)
        result.append(InterviewSource(interview, tuple(reasons[interview.pk]), tuple(latest)))
    return result
