"""報酬の説明（参考様式第1-4号の元データ。T34-D2、ADR-0023）。

雇用契約の版と 1 対 1 の別の表（ContractWageExplanation）。版と違って編集できる（作成・更新は
監査ログに残る）。新しい版を作るときは、前の版の説明を写す（create_version から copy_to を呼ぶ）。
契約の版の差分・届出の判定・「差分の無い版は保存しない」には関わらない。

生成前の検査で使う判定:
- missing_items: 比較の型と、その型で書く欄が埋まっているか（「報酬根拠未入力」。生成不可）。
- lower_than_japanese: 比較対象の日本人の報酬・賃金規程の報酬が申請人の報酬を上回るか（様式の ２
  だけ。単位をそろえて比べる。役職・経験の違いはシステムでは判断できないので強い警告。基準省令
  第1条第1項第3号）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.utils.translation import gettext as _

from apps.contracts.models import (
    ContractWageExplanation,
    EmploymentContractVersion,
    WageComparison,
    WageType,
    WageUnit,
)
from apps.core.models import User

COPIED_FIELDS = (
    "comparison",
    "wage_unit",
    "applicant_duties",
    "applicant_experience_years",
    "applicant_other",
    "japanese_duties",
    "japanese_age",
    "japanese_sex",
    "japanese_experience_years",
    "japanese_wage",
    "has_wage_rules",
    "rules_wage",
    "reason",
    "other",
)


def explanation_of(version: EmploymentContractVersion) -> ContractWageExplanation | None:
    return ContractWageExplanation.objects.filter(version=version).first()


def initial_values(version: EmploymentContractVersion) -> dict[str, Any]:
    """まだ説明の無い版の入力の初期値（版の「同等報酬」のメモと額。保存はしない）。"""
    values: dict[str, Any] = {}
    if version.comparable_japanese_note:
        values["japanese_duties"] = version.comparable_japanese_note
    if version.comparable_japanese_wage is not None:
        values["japanese_wage"] = version.comparable_japanese_wage
        values["wage_unit"] = WageUnit.MONTHLY
    return values


@transaction.atomic
def save(
    version: EmploymentContractVersion, values: dict[str, Any], *, by: User | None
) -> ContractWageExplanation:
    """説明を作る・直す（監査ログはモデルの保存で残る）。"""
    explanation = explanation_of(version) or ContractWageExplanation(version=version, created_by=by)
    for name in COPIED_FIELDS:
        if name in values:
            setattr(explanation, name, values[name])
    explanation.updated_by = by
    explanation.full_clean()
    explanation.save()
    return explanation


def copy_to(
    previous: EmploymentContractVersion, version: EmploymentContractVersion, *, by: User | None
) -> ContractWageExplanation | None:
    """新しい版に前の版の説明を写す（前の版に説明が無ければ何もしない）。"""
    source = explanation_of(previous)
    if source is None:
        return None
    return ContractWageExplanation.objects.create(
        version=version,
        created_by=by,
        updated_by=by,
        **{name: getattr(source, name) for name in COPIED_FIELDS},
    )


def applicant_wage(version: EmploymentContractVersion, unit: str) -> int | None:
    """申請人の報酬（第1-4号の単位にそろえる。月給・時間給以外は換算額。注意３）。"""
    if unit == WageUnit.MONTHLY:
        if version.wage_type == WageType.MONTHLY:
            return version.base_wage
        return version.monthly_equivalent
    if unit == WageUnit.HOURLY:
        if version.wage_type == WageType.HOURLY:
            return version.base_wage
        return version.hourly_equivalent
    return None


@dataclass(frozen=True)
class Missing:
    code: str
    message: str


def missing_items(
    version: EmploymentContractVersion, explanation: ContractWageExplanation | None
) -> list[Missing]:
    """「報酬根拠未入力」: 比較の型と、その型で様式に書く欄（生成不可）。"""
    if explanation is None or not explanation.comparison:
        return [Missing("wage_basis_missing", _("The wage explanation (form 1-4) is not entered."))]
    if explanation.comparison == WageComparison.NONE:
        return [
            Missing(
                "wage_basis_no_japanese",
                _(
                    "There is no Japanese worker to compare with, so form 1-4 cannot be used. "
                    "Explain in a document of any format how the wage is equal to or higher "
                    "than that of a Japanese worker."
                ),
            )
        ]
    labels = {
        "wage_unit": _("unit of the wages on the form"),
        "applicant_duties": _("applicant: position, duties and degree of responsibility"),
        "applicant_experience_years": _("applicant: years of experience in the duties"),
        "japanese_duties": _("Japanese worker: position, duties and degree of responsibility"),
        "japanese_age": _("Japanese worker: age"),
        "japanese_sex": _("Japanese worker: sex"),
        "japanese_experience_years": _("Japanese worker: years of experience"),
        "japanese_wage": _("Japanese worker: wage"),
        "has_wage_rules": _("there are wage rules"),
        "reason": _("reason the wage is equal to or higher than that of a Japanese worker"),
    }
    result = [
        Missing("wage_basis_item", _('"%(label)s" is empty.') % {"label": label})
        for name, label in labels.items()
        if getattr(explanation, name) in (None, "")
    ]
    if explanation.has_wage_rules and explanation.rules_wage is None:
        result.append(
            Missing(
                "wage_basis_item",
                _('"%(label)s" is empty.') % {"label": _("wage under the wage rules")},
            )
        )
    if explanation.wage_unit and applicant_wage(version, explanation.wage_unit) is None:
        result.append(
            Missing(
                "wage_basis_conversion",
                _(
                    "The applicant's wage cannot be written as a %(unit)s: enter the equivalent "
                    "amount in the contract version."
                )
                % {"unit": WageUnit(explanation.wage_unit).label},
            )
        )
    return result


def lower_than_japanese(
    version: EmploymentContractVersion, explanation: ContractWageExplanation | None
) -> list[Missing]:
    """比較対象の日本人の報酬・賃金規程の報酬が申請人の報酬を上回る（強い警告）。

    様式の ２（比較対象となる日本人労働者がいる場合）だけで比べる。３（最も近い職務を担う日本人）は
    報酬が大きく異なる日本人と比べる欄で、記載例も申請人より高い報酬を書いている（ADR-0023）。
    """
    if explanation is None or explanation.comparison != WageComparison.COMPARABLE:
        return []
    applicant = applicant_wage(version, explanation.wage_unit)
    if applicant is None:
        return []
    result = []
    unit = WageUnit(explanation.wage_unit).label
    if explanation.japanese_wage is not None and explanation.japanese_wage > applicant:
        result.append(
            Missing(
                "wage_below_japanese",
                _(
                    "The applicant's wage (%(applicant)s yen, %(unit)s) is lower than that of the "
                    "Japanese worker (%(japanese)s yen). The wage must be equal to or higher than "
                    "that of a Japanese worker; explain the difference in position or experience."
                )
                % {
                    "applicant": f"{applicant:,}",
                    "unit": unit,
                    "japanese": f"{explanation.japanese_wage:,}",
                },
            )
        )
    if (
        explanation.has_wage_rules
        and explanation.rules_wage is not None
        and explanation.rules_wage > applicant
    ):
        result.append(
            Missing(
                "wage_below_rules",
                _(
                    "The applicant's wage (%(applicant)s yen, %(unit)s) is lower than the wage "
                    "under the wage rules (%(rules)s yen)."
                )
                % {
                    "applicant": f"{applicant:,}",
                    "unit": unit,
                    "rules": f"{explanation.rules_wage:,}",
                },
            )
        )
    return result
