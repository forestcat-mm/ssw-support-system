"""申請の書類の一覧からの、不足の書類の一括生成と行ごとの検査（T42、ADR-0033）。

- 対象は、当てはまる行のうち生成する様式で、未生成か、ほかの契約・版を対象にした生成物しか無い行
  （T42-D8）。判断できない行・システムの外の行・添付の行は生成しない（添付の不足は一覧で知らせる）。
- 入力は 1 件ずつの生成の画面の初期値と同じ（T42-D9）: 基準日は一覧の基準日、作成責任者は既定、
  言語は契約の版の様式だけ支援言語から（form_languages.default_for）。第1-17号のⅢ欄７の申告は
  一括の画面の印を使う。
- 同じ様式の行（第1-6号と別紙）は 1 回だけ生成する。
- 生成不可（検査）・様式が読み込まれていない・計画や契約が無いものは飛ばして返す。警告は
  accept_warnings=True のときだけ生成する（form_generation.request と同じ）。
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import User
from apps.forms_engine.models import GeneratedDocument
from apps.support.models import SupportPlan
from services import (
    application_sets,
    form_checks,
    form_data_1_17,
    form_data_application,
    form_data_contract,
    form_generation,
    form_languages,
    form_templates,
    support_plans,
)
from services.application_sets import Overview, RowState, Status

TARGET_STATUSES = frozenset({Status.NOT_GENERATED, Status.OTHER_TARGET})
# 不足として一括の画面に出す添付の行の状態
ATTACHMENT_GAPS = frozenset(
    {Status.MISSING, Status.EXPIRED, Status.OUT_OF_PERIOD, Status.DATE_MISSING}
)
# 作成責任者を入れる様式（契約を対象にする申請の書類。edit_views.AUTHOR_FORMS と同じ）
AUTHOR_FORMS = frozenset({"3-1", "13-1", "13-2", "1-25", "1-11-1"})
# 生成の画面で入れる申告の規則（一覧の行の検査の結果には出さない）
SCREEN_INPUT_RULES = frozenset({"propriety"})


class Problem:
    NO_PLAN = "no_plan"  # 支援計画（の版）が無い
    NO_CONTRACT = "no_contract"  # 基準日の雇用契約が無い
    NOT_LOADED = "not_loaded"  # 様式が読み込まれていない
    NEEDS_INPUT = "needs_input"  # 理由などを入れる様式（1 件ずつの画面で作る。第1-26号）


PROBLEM_TEXTS = {
    Problem.NO_PLAN: _("There is no support plan."),
    Problem.NO_CONTRACT: _("There is no employment contract on the basis date."),
    Problem.NOT_LOADED: _("The form is not loaded."),
    Problem.NEEDS_INPUT: _(
        "This form needs input such as the reason. Generate it on its own page."
    ),
}


def problem_text(code: str) -> str:
    return str(PROBLEM_TEXTS[code])


@dataclass
class Candidate:
    """生成する行 1 つ（検査の結果つき）。"""

    state: RowState
    form_id: str
    target: models.Model | None = None
    data: dict[str, Any] = field(default_factory=dict)
    language: str = form_languages.JA
    result: form_checks.CheckResult | None = None
    problem: str = ""
    same_as: str = ""  # 同じ様式を先に生成する行の ID（第1-6号と別紙）

    @property
    def row_id(self) -> str:
        return str(self.state.row["id"])

    @property
    def blocked(self) -> bool:
        return bool(self.problem) or (self.result is not None and bool(self.result.blocking))

    @property
    def blocking(self) -> list[form_checks.Finding]:
        return self.result.blocking if self.result else []

    @property
    def warnings(self) -> list[form_checks.Finding]:
        return self.result.warnings if self.result else []

    @property
    def problem_message(self) -> str:
        return problem_text(self.problem) if self.problem else ""


def _target(form_id: str, overview: Overview) -> tuple[models.Model | None, str]:
    facts = overview.facts
    if form_id in application_sets.WORKER_FORMS:
        return None, Problem.NEEDS_INPUT
    if form_id == "1-17":
        plan = SupportPlan.objects.filter(worker=facts.worker).order_by("-pk").first()
        version = (
            (support_plans.current_version(plan, facts.on) or support_plans.latest_version(plan))
            if plan
            else None
        )
        return (version, "") if version else (None, Problem.NO_PLAN)
    if facts.contract is None or facts.version is None:
        return None, Problem.NO_CONTRACT
    if form_id in application_sets.CONTRACT_VERSION_FORMS:
        return facts.version, ""
    return facts.contract, ""


def _data(form_id: str, target: Any, on: datetime.date, propriety: bool) -> dict[str, Any]:
    if form_id == "1-17":
        return form_data_1_17.build(target, on) | form_data_1_17.declaration_values(propriety)
    if form_id == "1-6":
        return form_data_contract.build_1_6(target)
    if form_id == "1-5":
        return form_data_contract.build_1_5(target)
    if form_id == "1-4":
        return form_data_contract.build_1_4(
            target, on, author=form_data_contract.default_author(target)
        )
    author = (
        form_data_application.default_author(form_id, target) if form_id in AUTHOR_FORMS else ""
    )
    return form_data_application.build(form_id, target, on, author=author)


def candidates(overview: Overview, *, propriety: bool = False) -> list[Candidate]:
    """一括で生成する行と、その検査の結果（表の順）。"""
    found: list[Candidate] = []
    first_of: dict[str, str] = {}
    for state in overview.rows:
        form_id = state.row["how"].get("generate")
        if not form_id or state.applicable is not True or state.status not in TARGET_STATUSES:
            continue
        candidate = Candidate(state=state, form_id=form_id)
        found.append(candidate)
        if form_id in first_of:
            candidate.same_as = first_of[form_id]
            continue
        first_of[form_id] = candidate.row_id
        target, problem = _target(form_id, overview)
        if target is None:
            candidate.problem = problem
            continue
        on = overview.facts.on
        candidate.target = target
        if form_id in application_sets.CONTRACT_VERSION_FORMS:
            candidate.language = form_languages.default_for(overview.facts.worker, form_id)
        candidate.data = _data(form_id, target, on, propriety)
        try:
            candidate.result = form_generation.check(
                form_id,
                target=target,
                data=candidate.data,
                basis_on=on,
                language=candidate.language,
            )
        except form_templates.FormTemplateError:
            candidate.problem = Problem.NOT_LOADED
    # 同じ様式の後の行は、先の行の結果を見せる
    by_id = {candidate.row_id: candidate for candidate in found}
    for candidate in found:
        if candidate.same_as:
            first = by_id[candidate.same_as]
            candidate.result, candidate.problem = first.result, first.problem
    return found


@dataclass(frozen=True)
class RowCheck:
    """一覧の行に出す検査の結果（生成の画面で入れる申告の規則は除く）。"""

    blocking: int
    warnings: int
    problem: str = ""

    @property
    def blocked(self) -> bool:
        return bool(self.problem) or self.blocking > 0


def row_checks(overview: Overview) -> dict[str, RowCheck]:
    """申請の書類の一覧の行ごとの検査の結果（未生成・ほかの契約の行。T42 の一覧の見分け）。"""
    result: dict[str, RowCheck] = {}
    for candidate in candidates(overview, propriety=True):
        warnings = [w for w in candidate.warnings if w.rule not in SCREEN_INPUT_RULES]
        result[candidate.row_id] = RowCheck(
            blocking=len(candidate.blocking), warnings=len(warnings), problem=candidate.problem
        )
    return result


def attachment_gaps(overview: Overview) -> list[RowState]:
    """添付の行のうち、無い・期限切れ・時期の外・受診日の無いもの（一括の画面の「不足」）。"""
    return [
        state
        for state in overview.rows
        if "attachment" in state.row["how"]
        and state.applicable is True
        and state.status in ATTACHMENT_GAPS
    ]


@dataclass
class Outcome:
    requested: list[tuple[Candidate, GeneratedDocument]] = field(default_factory=list)
    skipped: list[Candidate] = field(default_factory=list)


def generate(
    found: Iterable[Candidate],
    selected: Iterable[str],
    *,
    basis_on: datetime.date,
    by: User | None,
    accept_warnings: bool,
) -> Outcome:
    """選んだ行を生成する（生成不可・様式の無いものは飛ばす）。

    警告のある行を選んで accept_warnings が無ければ、何も生成せずに WarningsNotAccepted を送る。
    """
    chosen = set(selected)
    rows = [c for c in found if c.row_id in chosen and not c.same_as]
    outcome = Outcome()
    if not accept_warnings:
        pending = [c for c in rows if not c.blocked and c.warnings]
        if pending:
            assert pending[0].result is not None
            raise form_generation.WarningsNotAccepted(pending[0].result)
    for candidate in rows:
        if candidate.blocked or candidate.target is None:
            outcome.skipped.append(candidate)
            continue
        try:
            document = form_generation.request(
                candidate.form_id,
                target=candidate.target,
                data=candidate.data,
                basis_on=basis_on,
                by=by,
                language=candidate.language,
                accept_warnings=accept_warnings,
            )
        except form_generation.GenerationBlocked as blocked:
            candidate.result = blocked.result  # 検査の後にデータが変わった
            outcome.skipped.append(candidate)
        except form_templates.FormTemplateError:
            candidate.problem = Problem.NOT_LOADED
            outcome.skipped.append(candidate)
        else:
            outcome.requested.append((candidate, document))
    return outcome
