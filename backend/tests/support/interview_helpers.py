"""定期面談のテスト用ヘルパ（T35 から報告書の回答が必須。ADR-0024）。"""

from dataclasses import replace
from datetime import date
from typing import Any

from services import interview_answers
from services.interview_answers import AnswerSetInput, NonconformityInput
from services.support_interviews import InterviewInput


def nonconformity_for(data: InterviewInput, on: date) -> NonconformityInput:
    """違反の疑いが「あり」なら ⑥ を「有り」にして ４ を埋める（無ければ「なし」）。"""
    if not data.violation_suspected:
        return NonconformityInput(exists=False)
    return NonconformityInput(
        exists=True,
        occurred_on=on,
        content="割増賃金の一部が支払われていない。",
        worker_guidance="guided",
        guided_to="労働基準監督署",
        host_notified=False,
        not_notified_reason="事実を確認中。",
        immigration_guided=False,
        unreported_reason="事実を確認中。",
    )


def answered(data: InterviewInput) -> InterviewInput:
    """回答が無ければ、全部「問題無」の回答と ⑥ を足す（オンラインの行は録画の保存先も）。"""
    on = min(data.self_row.occurred_on, data.supervisor_row.occurred_on)

    def answers(row: Any) -> AnswerSetInput:
        location = "社内の共有フォルダ" if row.method == "online" else ""
        return AnswerSetInput(answers=interview_answers.all_no(on), recording_location=location)

    return replace(
        data,
        self_answers=data.self_answers or answers(data.self_row),
        supervisor_answers=data.supervisor_answers or answers(data.supervisor_row),
        nonconformity=data.nonconformity or nonconformity_for(data, on),
    )


def answer_fields(on: date, value: str = "no") -> dict[str, str]:
    """画面の POST の回答の欄（2 行とも全部「問題無」・⑥「なし」）。"""
    fields = {"nonconformity-exists": "no"}
    for prefix in ("self_answers", "supervisor_answers"):
        for code in interview_answers.version_for(on).codes:
            fields[f"{prefix}-answer_{code.replace('.', '_')}"] = value
    return fields
