"""定期面談報告書（第5-5号・第5-6号）の回答（T35、ADR-0024）。

- 質問は版つきのデータ（services/data/interview_questions.json）。本人用と監督者用で同じ符号、文言が
  違う。記録した版を回答の組に残す。
- T35 から記録する面談は、2 行とも全部の質問に「問題の有無」を答える（T35-D2）。「有」なら内容が
  必須。5.2（その他）が「有」なら、その事項も必須。
- 「⑥ 基準不適合等の有無」は組に 1 つ。労働法令違反の疑いが「あり」なら ⑥ は「有り」（T35-D4）。
  「有り」なら ４（発生年月日・内容・対応結果）を書く。
- オンラインの行は録画の保存先が必須（T35-D8。別冊 32頁）。
- T35 より前の記録には回答が無い（後から足さない。T35-D3）。
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

from apps.core.models import User
from apps.support.models import (
    InterviewAnswer,
    InterviewAnswerSet,
    InterviewNonconformity,
    PeriodicInterview,
    RecordMethod,
    SupportRecord,
    WorkerGuidance,
)

DATA_FILE = Path(__file__).resolve().parent / "data" / "interview_questions.json"
OTHER_CODE = "5.2"


@dataclass(frozen=True)
class Question:
    code: str
    self_text: str  # 本人用（第5-5号）
    supervisor_text: str  # 監督者用（第5-6号）
    other: bool = False  # 事項を自由に書く行（5.2 その他）


@dataclass(frozen=True)
class Section:
    code: str
    title: str
    questions: tuple[Question, ...]


@dataclass(frozen=True)
class QuestionVersion:
    id: str
    effective_from: datetime.date
    sections: tuple[Section, ...]

    @property
    def questions(self) -> tuple[Question, ...]:
        return tuple(q for section in self.sections for q in section.questions)

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(q.code for q in self.questions)


@cache
def versions() -> tuple[QuestionVersion, ...]:
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    result = [
        QuestionVersion(
            id=raw["id"],
            effective_from=datetime.date.fromisoformat(raw["effective_from"]),
            sections=tuple(
                Section(
                    code=section["code"],
                    title=section["title"],
                    questions=tuple(
                        Question(
                            code=q["code"],
                            self_text=q["self"],
                            supervisor_text=q["supervisor"],
                            other=bool(q.get("other")),
                        )
                        for q in section["questions"]
                    ),
                )
                for section in raw["sections"]
            ),
        )
        for raw in data["versions"]
    ]
    return tuple(sorted(result, key=lambda version: version.effective_from))


def version_for(on: datetime.date) -> QuestionVersion:
    """その日に適用される質問の版（適用開始より前の日なら最も古い版）。"""
    applied = [version for version in versions() if version.effective_from <= on]
    return applied[-1] if applied else versions()[0]


def version_of(answer_set: InterviewAnswerSet) -> QuestionVersion:
    return next(version for version in versions() if version.id == answer_set.question_version)


# --- 入力 ----------------------------------------------------------------------------


@dataclass
class AnswerInput:
    has_problem: bool | None
    detail: str = ""


@dataclass
class AnswerSetInput:
    """1 行（本人用・監督者用）の回答。answers は質問の符号ごと。"""

    answers: dict[str, AnswerInput] = field(default_factory=dict)
    other_subject: str = ""
    notable: str = ""
    supervisor_position: str = ""
    supervisor_department: str = ""
    recording_location: str = ""


@dataclass
class NonconformityInput:
    """⑥ と ４（組に 1 つ）。exists が None なら未回答。"""

    exists: bool | None = None
    occurred_on: datetime.date | None = None
    content: str = ""
    worker_guidance: str = ""
    guided_to: str = ""
    no_guidance_reason: str = ""
    host_notified: bool | None = None
    notified_on: datetime.date | None = None
    notified_to: str = ""
    not_notified_reason: str = ""
    immigration_guided: bool | None = None
    unreported_reason: str = ""


def all_no(on: datetime.date) -> dict[str, AnswerInput]:
    """全部の質問を「無」にした回答（画面の「すべて無にする」と同じ）。"""
    return {code: AnswerInput(False) for code in version_for(on).codes}


# --- 検査 ----------------------------------------------------------------------------


def answer_errors(
    prefix: str, data: AnswerSetInput, *, method: str, on: datetime.date
) -> dict[str, list[str]]:
    """1 行の回答の不足（キーは "<prefix>.answer.<符号>" など）。"""
    errors: dict[str, list[str]] = {}

    def add(key: str, message: str) -> None:
        errors.setdefault(f"{prefix}.{key}", []).append(message)

    version = version_for(on)
    for question in version.questions:
        answer = data.answers.get(question.code)
        if answer is None or answer.has_problem is None:
            add(f"answer.{question.code}", _("Select whether there is a problem."))
            continue
        if answer.has_problem and not answer.detail.strip():
            add(f"detail.{question.code}", _("Enter the details of the problem."))
        if question.other and answer.has_problem and not data.other_subject.strip():
            add("other_subject", _("Enter the other matter."))
    unknown = set(data.answers) - set(version.codes)
    if unknown:
        add("answer", _("Unknown questions: %(codes)s") % {"codes": ", ".join(sorted(unknown))})
    if method == RecordMethod.ONLINE and not data.recording_location.strip():
        add(
            "recording_location",
            _("Enter where the recording of the online interview is stored."),
        )
    return errors


def nonconformity_errors(
    data: NonconformityInput,
    *,
    violation_suspected: bool,
    reported: bool,
    today: datetime.date,
) -> dict[str, list[str]]:
    """⑥ と ４ の不足（キーは "nonconformity.<欄>"）。reported は通報済み（４③ウ）。"""
    errors: dict[str, list[str]] = {}

    def add(key: str, message: str) -> None:
        errors.setdefault(f"nonconformity.{key}", []).append(message)

    if data.exists is None:
        add("exists", _("Select whether there is a non-conformity with the standards."))
        return errors
    if violation_suspected and not data.exists:
        add(
            "exists",
            _(
                'A violation of labour laws is suspected, so select "yes" for non-conformity '
                "with the standards."
            ),
        )
    detail_values = (
        data.occurred_on,
        data.content.strip(),
        data.worker_guidance,
        data.guided_to.strip(),
        data.no_guidance_reason.strip(),
        data.host_notified,
        data.notified_on,
        data.notified_to.strip(),
        data.not_notified_reason.strip(),
        data.immigration_guided,
        data.unreported_reason.strip(),
    )
    if not data.exists:
        if any(value not in (None, "") for value in detail_values):
            add("exists", _("Enter section 4 only when there is a non-conformity."))
        return errors
    if data.occurred_on is None:
        add("occurred_on", _("Enter the date."))
    elif data.occurred_on > today:
        add("occurred_on", _("The date cannot be in the future."))
    if not data.content.strip():
        add("content", _("Enter the details."))
    if data.worker_guidance not in WorkerGuidance.values:
        add("worker_guidance", _("Select the action for the worker."))
    elif data.worker_guidance == WorkerGuidance.GUIDED and not data.guided_to.strip():
        add("guided_to", _("Enter the authority."))
    elif data.worker_guidance == WorkerGuidance.NONE and not data.no_guidance_reason.strip():
        add("no_guidance_reason", _("Enter the reason."))
    if data.host_notified is None:
        add("host_notified", _("Select whether the host was notified."))
    elif data.host_notified:
        if data.notified_on is None:
            add("notified_on", _("Enter the date."))
        elif data.notified_on > today:
            add("notified_on", _("The date cannot be in the future."))
        if not data.notified_to.strip():
            add("notified_to", _("Enter who was notified."))
    elif not data.not_notified_reason.strip():
        add("not_notified_reason", _("Enter the reason."))
    if data.immigration_guided is None:
        add("immigration_guided", _("Select whether the host was guided to the agency."))
    if not reported and not data.unreported_reason.strip():
        add(
            "unreported_reason",
            _("Enter why the report to the authorities has not been made (including not needed)."),
        )
    return errors


def raise_errors(errors: dict[str, list[str]]) -> None:
    if errors:
        raise ValidationError(
            {
                key: [ValidationError(message) for message in messages]
                for key, messages in errors.items()
            }
        )


# --- 保存 ----------------------------------------------------------------------------


def save_answers(
    record: SupportRecord, data: AnswerSetInput, *, by: User | None
) -> InterviewAnswerSet:
    """検査済みの回答を保存する（呼び出し側のトランザクションの中で）。"""
    version = version_for(record.occurred_on)
    answer_set = InterviewAnswerSet.objects.create(
        record=record,
        question_version=version.id,
        other_subject=data.other_subject.strip(),
        notable=data.notable.strip(),
        supervisor_position=data.supervisor_position.strip(),
        supervisor_department=data.supervisor_department.strip(),
        recording_location=data.recording_location.strip(),
        created_by=by,
        updated_by=by,
    )
    for code in version.codes:
        answer = data.answers[code]
        InterviewAnswer.objects.create(
            answer_set=answer_set,
            code=code,
            has_problem=bool(answer.has_problem),
            detail=answer.detail.strip() if answer.has_problem else "",
            created_by=by,
            updated_by=by,
        )
    return answer_set


def save_nonconformity(
    interview: PeriodicInterview, data: NonconformityInput, *, by: User | None
) -> InterviewNonconformity:
    values: dict[str, Any] = {
        "exists": bool(data.exists),
        "occurred_on": data.occurred_on,
        "content": data.content.strip(),
        "worker_guidance": data.worker_guidance,
        "guided_to": data.guided_to.strip()
        if data.worker_guidance == WorkerGuidance.GUIDED
        else "",
        "no_guidance_reason": (
            data.no_guidance_reason.strip() if data.worker_guidance == WorkerGuidance.NONE else ""
        ),
        "host_notified": data.host_notified,
        "notified_on": data.notified_on if data.host_notified else None,
        "notified_to": data.notified_to.strip() if data.host_notified else "",
        "not_notified_reason": (
            data.not_notified_reason.strip() if data.host_notified is False else ""
        ),
        "immigration_guided": data.immigration_guided,
        "unreported_reason": data.unreported_reason.strip(),
    }
    return InterviewNonconformity.objects.create(
        interview=interview, created_by=by, updated_by=by, **values
    )


# --- 読み出し --------------------------------------------------------------------------


def answers_of(record: SupportRecord) -> InterviewAnswerSet | None:
    return InterviewAnswerSet.objects.filter(record=record).first()


def nonconformity_of(interview: PeriodicInterview) -> InterviewNonconformity | None:
    return InterviewNonconformity.objects.filter(interview=interview).first()


def has_answers(interview: PeriodicInterview) -> bool:
    return InterviewAnswerSet.objects.filter(record__interview=interview).exists()


@dataclass(frozen=True)
class AnswerRow:
    """画面・帳票に出す 1 問。"""

    question: Question
    text: str
    has_problem: bool
    detail: str


def rows_of(answer_set: InterviewAnswerSet, *, supervisor: bool) -> list[AnswerRow]:
    version = version_of(answer_set)
    saved = {answer.code: answer for answer in answer_set.answers.all()}
    result = []
    for question in version.questions:
        answer = saved.get(question.code)
        if answer is None:
            continue
        result.append(
            AnswerRow(
                question=question,
                text=question.supervisor_text if supervisor else question.self_text,
                has_problem=answer.has_problem,
                detail=answer.detail,
            )
        )
    return result


def problems(interview: PeriodicInterview) -> int:
    """「有」の数（2 行の合計）。"""
    return InterviewAnswer.objects.filter(
        answer_set__record__interview=interview, has_problem=True
    ).count()
