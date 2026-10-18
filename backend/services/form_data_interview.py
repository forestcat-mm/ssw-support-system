"""定期面談報告書（第5-5号 本人用・第5-6号 監督者用）のデータを、面談の組から作る（T35、ADR-0024）。

- 対象は面談の組。第5-5号は本人用の行、第5-6号は監督者用の行の回答を書く。⑥ と ４ は組に 1 つで、
  両方の様式に同じものを書く。
- 回答の無い記録（T35 より前）は、質問・⑥・４ の欄を書かない（印刷済みの □ のまま。生成前の警告）。
- 作成年月日は生成した日（記載例 ★４「本届出書を作成した日付」）。
- キーは tools/forms/make_interview_mappings.py の欄の名前に「i.」を付けたもの。
"""

from __future__ import annotations

import datetime
from typing import Any

from apps.support.models import (
    Counterpart,
    PeriodicInterview,
    RecordMethod,
    SupportRecord,
    WorkerGuidance,
)
from services import contracts, interview_answers

FORM_ROWS = {"5-5": Counterpart.WORKER, "5-6": Counterpart.SUPERVISOR}


def record_for(interview: PeriodicInterview, form_id: str) -> SupportRecord | None:
    return interview.records.filter(counterpart=FORM_ROWS[form_id]).order_by("pk").first()


def host_name(interview: PeriodicInterview) -> str:
    contract = contracts.contract_on(interview.worker, interview.basis_on)
    return contract.host_organization.name if contract is not None else ""


def _answers(record: SupportRecord) -> dict[str, Any]:
    answer_set = interview_answers.answers_of(record)
    if answer_set is None:
        return {}
    data: dict[str, Any] = {
        "other_subject": answer_set.other_subject,
        "notable": answer_set.notable,
    }
    for answer in answer_set.answers.all():
        name = answer.code.replace(".", "_")
        data[f"q{name}_yes"] = answer.has_problem
        data[f"q{name}_no"] = not answer.has_problem
        data[f"q{name}_detail"] = answer.detail
    return data


def _nonconformity(interview: PeriodicInterview) -> dict[str, Any]:
    found = interview_answers.nonconformity_of(interview)
    if found is None:
        return {}
    data: dict[str, Any] = {"nc_yes": found.exists, "nc_no": not found.exists}
    if not found.exists:
        return data
    guided = found.worker_guidance == WorkerGuidance.GUIDED
    reported = bool(interview.report_required and interview.reported_on)
    report_to = interview.get_report_to_display() if interview.report_to else ""
    if interview.report_to_note:
        report_to = (
            f"{report_to}（{interview.report_to_note}）" if report_to else interview.report_to_note
        )
    data |= {
        "nc_occurred_on": found.occurred_on,
        "nc_content": found.content,
        "nc_guided": guided,
        "nc_guided_to": found.guided_to if guided else "",
        "nc_no_action": found.worker_guidance == WorkerGuidance.NONE,
        "nc_no_guidance_reason": found.no_guidance_reason,
        "nc_notified": found.host_notified is True,
        "nc_notified_on": found.notified_on,
        "nc_notified_to": found.notified_to,
        "nc_not_notified": found.host_notified is False,
        "nc_not_notified_reason": found.not_notified_reason,
        "nc_immigration_guided": found.immigration_guided is True,
        "nc_immigration_not_yet": found.immigration_guided is False,
        "nc_reported": reported,
        "nc_reported_on": interview.reported_on if reported else None,
        "nc_report_to": report_to if reported else "",
        "nc_unreported": not reported,
        "nc_unreported_reason": found.unreported_reason if not reported else "",
    }
    return data


def build(interview: PeriodicInterview, form_id: str, *, made_on: datetime.date) -> dict[str, Any]:
    record = record_for(interview, form_id)
    data: dict[str, Any] = {"made_on": made_on}
    if record is None:
        return {f"i.{key}": value for key, value in data.items()}
    staff = record.staff
    data |= {
        "interviewed_on": record.occurred_on,
        "method_in_person": record.method == RecordMethod.IN_PERSON,
        "method_online": record.method == RecordMethod.ONLINE,
        "staff_name": staff.name,
        "staff_manager": staff.is_support_manager,
        "staff_support": staff.is_support_staff,
        "staff_position": staff.position,
    }
    if form_id == "5-5":
        data |= {"worker_name": interview.worker.name_romaji, "host_name": host_name(interview)}
    else:
        answer_set = interview_answers.answers_of(record)
        position = answer_set.supervisor_position if answer_set else ""
        name = record.counterpart_name
        data |= {
            "supervisor_name": f"{name}（{position}）" if position else name,
            "supervisor_department": answer_set.supervisor_department if answer_set else "",
        }
    data |= _answers(record)
    data |= _nonconformity(interview)
    return {f"i.{key}": value for key, value in data.items()}
