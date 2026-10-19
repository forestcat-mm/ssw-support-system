"""帳票の生成前の検査（仕様書 第5章 F11、第6章「生成前バリデーション」。T30、ADR-0019）。

規則は様式の版ごとのデータ（mappings/<様式ID>/<適用開始日>.rules.json）で持ち、
読み込み（make forms）で FormMapping.rules に入る。規則の種類:

- required: データの値が空でない
- date: データの値が日付
- width: データの値が半角（half）または全角（full）だけ
- max_length: データの値の文字数が上限以下
- check: 名前で登録した判定の関数（下の @check）を呼ぶ

重さは block（生成不可）と warn（警告付きで生成）。規則の requires に別の規則の id を書くと、
その規則で不足が出なかったときだけ評価する（例: ガイダンスの記録が無い人に、欄ごとの不足を
重ねない）。
不足の 1 件ごとに、直す画面の行き先（URL の名前と対象の ID）を持つ。表示は T31 から。
"""

from __future__ import annotations

import datetime
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from functools import cached_property
from typing import Any

from django.db import models
from django.urls import NoReverseMatch, reverse
from django.utils.translation import gettext as _

from apps.contracts.models import (
    ConversionChange,
    EmploymentContract,
    EmploymentContractVersion,
    WageType,
)
from apps.core.models import AttachmentKind, Organization, Staff
from apps.hosts.models import HostOrganization, HostSite, SupportScope
from apps.support.models import (
    Briefing,
    DeliveryKind,
    DepositContract,
    PeriodicInterview,
    SupportPlanVersion,
)
from apps.workers.models import ApplicationType, ForeignWorker, LatestResidenceRecord
from services import briefings, contracts, deliveries, hosts
from services.support_catalog import Method, SupportItem, form_row_name

BLOCK = "block"
WARN = "warn"
SEVERITIES = (BLOCK, WARN)
FIELD_RULE_TYPES = ("required", "date", "width", "max_length")
RULE_TYPES = (*FIELD_RULE_TYPES, "check")


class RulesError(Exception):
    """規則のデータの不備（読み込みで止める）。"""


# --- 結果 -------------------------------------------------------------------------


@dataclass(frozen=True)
class Destination:
    """不足を直す画面（URL の名前と引数）。"""

    url_name: str
    kwargs: Mapping[str, Any] = field(default_factory=dict)

    def url(self) -> str:
        return reverse(self.url_name, kwargs=dict(self.kwargs))

    def as_json(self) -> dict[str, Any]:
        return {"url_name": self.url_name, "kwargs": dict(self.kwargs)}


@dataclass(frozen=True)
class Issue:
    """判定の関数が返す不足 1 件（重さは規則で決まる）。"""

    code: str
    message: str
    destination: Destination | None
    strong: bool = False


@dataclass(frozen=True)
class Finding:
    rule: str
    code: str
    severity: str
    message: str
    destination: Destination | None
    strong: bool = False

    def as_json(self) -> dict[str, Any]:
        """生成履歴に残す形（文言と行き先だけで、データの値は入れない）。"""
        return {
            "rule": self.rule,
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "strong": self.strong,
            "destination": self.destination.as_json() if self.destination else None,
        }


@dataclass(frozen=True)
class CheckResult:
    blocking: list[Finding]
    warnings: list[Finding]

    @property
    def findings(self) -> list[Finding]:
        return [*self.blocking, *self.warnings]


# --- 文脈 -------------------------------------------------------------------------


@dataclass
class CheckContext:
    """検査の対象（外国人・生成の対象の記録・基準日・様式に入れるデータ）。"""

    worker: ForeignWorker
    target: models.Model
    basis_on: datetime.date
    data: Mapping[str, Any]
    template: Any = None  # FormTemplate（欄の大きさを見る判定が使う）

    @cached_property
    def contract(self) -> EmploymentContract | None:
        """基準日の契約（雇用契約の書類は生成の対象の版の契約、自機関の書類は対象の契約。T34・T36）。"""
        if isinstance(self.target, EmploymentContractVersion):
            return self.target.contract
        if isinstance(self.target, EmploymentContract):
            return self.target
        return contracts.contract_on(self.worker, self.basis_on)

    @cached_property
    def version(self) -> EmploymentContractVersion | None:
        """基準日に有効な版（雇用契約の書類は、生成の対象の版。T34）。"""
        if isinstance(self.target, EmploymentContractVersion):
            return self.target
        return contracts.version_on(self.contract, self.basis_on) if self.contract else None

    @cached_property
    def site(self) -> HostSite | None:
        return self.version.site if self.version else None

    @cached_property
    def host(self) -> HostOrganization | None:
        return self.contract.host_organization if self.contract else None

    @property
    def plan_version(self) -> SupportPlanVersion | None:
        return self.target if isinstance(self.target, SupportPlanVersion) else None

    @property
    def briefing(self) -> Briefing | None:
        return self.target if isinstance(self.target, Briefing) else None

    @property
    def interview(self) -> PeriodicInterview | None:
        return self.target if isinstance(self.target, PeriodicInterview) else None

    @property
    def staff(self) -> Staff | None:
        return self.worker.assigned_staff


# 規則の fix.object → 行き先の URL の引数（データの規則の行き先。読み込みで reverse を確かめる）
def _object_kwargs(context: CheckContext, name: str) -> dict[str, int] | None:
    if name == "worker":
        return {"pk": context.worker.pk}
    if name == "briefing":
        briefing = context.briefing
        return {"pk": context.worker.pk, "briefing_id": briefing.pk} if briefing else None
    if name == "host":
        return {"pk": context.host.pk} if context.host else None
    if name == "staff":
        return {"pk": context.staff.pk} if context.staff else None
    if name == "organization":
        return {}
    if name == "contract_version":
        version = context.version
        if version is None:
            return None
        return {
            "pk": context.worker.pk,
            "contract_id": version.contract_id,
            "number": version.version,
        }
    raise RulesError(f"unknown fix object: {name}")


FIX_OBJECT_KWARGS: dict[str, dict[str, int]] = {
    "worker": {"pk": 1},
    "briefing": {"pk": 1, "briefing_id": 1},
    "host": {"pk": 1},
    "staff": {"pk": 1},
    "organization": {},
    "contract_version": {"pk": 1, "contract_id": 1, "number": 1},
}


# --- 判定の関数の表 -----------------------------------------------------------------

CheckFunction = Callable[[CheckContext, Mapping[str, Any]], list[Issue]]


@dataclass(frozen=True)
class RegisteredCheck:
    function: CheckFunction
    params: tuple[str, ...]


CHECKS: dict[str, RegisteredCheck] = {}


def check(name: str, *, params: Sequence[str] = ()) -> Callable[[CheckFunction], CheckFunction]:
    """判定の関数を名前で登録する（規則のデータの check.name から呼ぶ）。"""

    def register(function: CheckFunction) -> CheckFunction:
        CHECKS[name] = RegisteredCheck(function, tuple(params))
        return function

    return register


@check("employment_contract_on_basis")
def _contract_on_basis(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    if context.contract is not None:
        return []
    return [
        Issue(
            "contract_missing",
            _("There is no employment contract in effect on %(day)s (or starting after it).")
            % {"day": context.basis_on.isoformat()},
            Destination("workers:contracts", {"pk": context.worker.pk}),
        )
    ]


@check("membership_certificate")
def _membership_certificate(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """事業所の受入れ機関×分野の加入と証明書（ADR-0004。有効期限を過ぎた証明書は無いものとみなす）。

    契約が無いときは employment_contract_on_basis が知らせるので、ここでは何も出さない。
    """
    host, site = context.host, context.site
    if host is None or site is None:
        return []
    destination = Destination("hosts:detail", {"pk": host.pk})
    names = {"host": host.name, "body": site.field.membership_body_name}
    membership = host.memberships.filter(field=site.field).first()
    if membership is None or membership.joined_on > context.basis_on:
        message = _("%(host)s has no membership of %(body)s registered.") % names
        return [Issue("membership_missing", message, destination)]
    if membership.valid_until is not None and membership.valid_until < context.basis_on:
        message = _(
            "The membership of %(host)s in %(body)s expired on %(day)s. An expired certificate "
            "is treated as no certificate."
        ) % (names | {"day": membership.valid_until.isoformat()})
        return [Issue("membership_expired", message, destination)]
    certificate = membership.attachments.filter(
        kind=AttachmentKind.MEMBERSHIP_CERTIFICATE, voided_at__isnull=True
    )
    if not certificate.exists():
        message = _("The membership certificate of %(host)s (%(body)s) is not attached.") % names
        return [Issue("certificate_missing", message, destination)]
    return []


@check("residence_valid_on_basis")
def _residence_valid_on_basis(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """在留期限が基準日（申請日）より後（入国前の在留資格認定証明書の行は在留期限ではないので見ない）。"""
    destination = Destination("workers:history", {"pk": context.worker.pk})
    latest = (
        LatestResidenceRecord.objects.select_related("record").filter(worker=context.worker).first()
    )
    if latest is None:
        return [Issue("residence_missing", _("No residence record is registered."), destination)]
    record = latest.record
    if record.application_type == ApplicationType.CERTIFICATE_OF_ELIGIBILITY:
        return []
    if record.expires_on > context.basis_on:
        return []
    message = _("The period of stay expires on %(day)s, which is not after %(basis)s.") % {
        "day": record.expires_on.isoformat(),
        "basis": context.basis_on.isoformat(),
    }
    return [Issue("residence_expired", message, destination)]


@check("career_plan_delivered")
def _career_plan_delivered(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """基準日の契約のキャリアアッププランの交付（ADR-0010。転籍の前の契約は数えない）。"""
    if context.contract is None:
        return []
    destination = Destination("workers:support_deliveries", {"pk": context.worker.pk})
    return [
        Issue(found.warning.code, found.warning.message, destination, found.warning.strong)
        for found in deliveries.career_plan_findings(context.worker)
        if found.contract.pk == context.contract.pk
    ]


@check("support_language_staff")
def _support_language_staff(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """外国人の担当者の対応言語に、支援言語がある（通訳を手配するなら問題ない）。"""
    staff = context.staff
    if staff is None:
        return [
            Issue(
                "staff_missing",
                _("No staff member is assigned to this foreign worker."),
                Destination("workers:edit", {"pk": context.worker.pk}),
            )
        ]
    language = context.worker.support_language
    if staff.languages.filter(pk=language.pk).exists():
        return []
    message = _(
        "The assigned staff member (%(staff)s) is not registered as speaking the support "
        "language (%(language)s). This is not a problem if an interpreter is arranged."
    ) % {"staff": staff.name, "language": language.name}
    return [Issue("staff_language", message, Destination("settings:staff_edit", {"pk": staff.pk}))]


@check("briefing_record_exists", params=("kind",))
def _briefing_record_exists(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """生成の対象が、その種類の取り消していないガイダンス・オリエンテーションの記録である。"""
    kind = SupportItem(params.get("kind", SupportItem.PRE_GUIDANCE))
    briefing = context.briefing
    if briefing is not None and briefing.record.kind == kind and briefing.record.voided_at is None:
        return []
    return [
        Issue(
            "briefing_missing",
            _("There is no record of the %(kind)s.") % {"kind": kind.label},
            Destination(
                "workers:support_briefing_create", {"pk": context.worker.pk, "kind": kind.value}
            ),
        )
    ]


def _missing_by_section(briefing: Briefing) -> list[tuple[briefings.ChecklistSection, int]]:
    """確認書の欄ごとの、説明した・該当なしのどちらの印も無い細目の数。"""
    marked = set(briefing.checks.values_list("code", flat=True))
    result = []
    for section in briefings.checklist_of(briefing).sections:
        missing = sum(1 for item in section.items if item.code not in marked)
        if missing:
            result.append((section, missing))
    return result


@check("briefing_items_explained")
def _briefing_items_explained(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """確認書の固定の文は、番号の項目を「説明を受け、内容を十分に理解した」と述べる（ADR-0021）。

    その項目（確認書に番号のある欄）の細目に、説明した・該当なしのどちらの印も無いものがあれば、
    記録と食い違う。
    """
    briefing = context.briefing
    if briefing is None:
        return []
    numbers = [
        str(section.form_number)
        for section, _missing in _missing_by_section(briefing)
        if section.form_number is not None
    ]
    if not numbers:
        return []
    return [
        Issue(
            "items_not_explained",
            _(
                "Items %(numbers)s of the confirmation have sub-items marked neither as explained "
                "nor as not applicable, but the form states that they were explained."
            )
            % {"numbers": "・".join(numbers)},
            Destination(
                "workers:support_briefing", {"pk": context.worker.pk, "briefing_id": briefing.pk}
            ),
        )
    ]


@check("briefing_items_off_form")
def _briefing_items_off_form(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """確認書に番号の無い欄（例: 事前ガイダンスの 1.j）の未チェック（固定の文は述べない）。"""
    briefing = context.briefing
    if briefing is None:
        return []
    count = sum(
        missing for section, missing in _missing_by_section(briefing) if section.form_number is None
    )
    if not count:
        return []
    return [
        Issue(
            "items_off_form_missing",
            _("%(count)s sub-items not printed on the confirmation are not checked.")
            % {"count": count},
            Destination(
                "workers:support_briefing", {"pk": context.worker.pk, "briefing_id": briefing.pk}
            ),
        )
    ]


@check("briefing_no_deposit_contract")
def _briefing_no_deposit_contract(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """第5-9号の 4 と末尾の文は「保証金等の契約をしていない」と述べる（記録が「有」なら食い違う）"""
    briefing = context.briefing
    if briefing is None or briefing.deposit_contract != DepositContract.EXISTS:
        return []
    return [
        Issue(
            "deposit_contract_exists",
            _(
                "The record says there is a contract on deposits or penalties, but the "
                "confirmation (item 4 and the last sentence) states that there is none."
            ),
            Destination(
                "workers:support_briefing", {"pk": context.worker.pk, "briefing_id": briefing.pk}
            ),
            strong=True,
        )
    ]


@check("briefing_warnings", params=("exclude",))
def _briefing_warnings(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """ガイダンス・オリエンテーションの記録の注意（T21）。exclude のコードは出さない。"""
    briefing = context.briefing
    if briefing is None:
        return []
    excluded = set(params.get("exclude", []))
    destination = Destination(
        "workers:support_briefing", {"pk": context.worker.pk, "briefing_id": briefing.pk}
    )
    return [
        Issue(warning.code, warning.message, destination, warning.strong)
        for warning in briefings.warnings(briefing)
        if warning.code not in excluded
    ]


@check("contract_not_past_end")
def _contract_not_past_end(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """基準日の契約が、期間の終了日を過ぎて終了を登録していないもの（T30 から。T31）。"""
    contract = context.contract
    if contract is None or contract.terminated_on is not None or contract.current_ends_on is None:
        return []
    if contract.current_ends_on >= context.basis_on:
        return []
    return [
        Issue(
            "contract_past_end",
            _(
                "The employment contract period ended on %(day)s. Register a new version for "
                "the renewal or the termination."
            )
            % {"day": contract.current_ends_on.isoformat()},
            Destination("workers:contracts", {"pk": context.worker.pk}),
        )
    ]


@check("support_contract_on_basis")
def _support_contract_on_basis(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """計画の受入れ機関（無ければ基準日の契約の受入れ機関）と、基準日に支援委託契約がある。"""
    version = context.plan_version
    host = version.plan.host_organization if version is not None else context.host
    if host is None or hosts.active_contract(host, context.basis_on) is not None:
        return []
    return [
        Issue(
            "support_contract_missing",
            _("%(host)s has no support contract in effect on %(day)s.")
            % {"host": host.name, "day": context.basis_on.isoformat()},
            Destination("hosts:detail", {"pk": host.pk}),
        )
    ]


@check("plan_not_outsourced")
def _plan_not_outsourced(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """計画の行の「委託の有無」が「無」（全部委託では「無」。第1-17号 注意５）。"""
    version = context.plan_version
    if version is None:
        return []
    codes = list(
        version.activities.filter(outsourced=True).order_by("code").values_list("code", flat=True)
    )
    if not codes:
        return []
    return [
        Issue(
            "plan_outsourced",
            _(
                "Rows %(codes)s are marked as outsourced. When all support is entrusted to a "
                'registered support organization, every row must be "not outsourced".'
            )
            % {"codes": "、".join(form_row_name(code) for code in codes)},
            Destination("workers:support", {"pk": context.worker.pk}),
        )
    ]


@check("plan_orientation_other")
def _plan_orientation_other(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """Ⅳ欄４（生活オリエンテーション）の「その他」は、様式に欄が無く出ない（T31-D10）。"""
    version = context.plan_version
    if version is None:
        return []
    row = version.activities.filter(code="4.main").first()
    if row is None or Method.OTHER not in row.methods:
        return []
    return [
        Issue(
            "orientation_other",
            _(
                'The form has no "other" box for the method of the life orientation, so it '
                "will not be printed."
            ),
            Destination("workers:support", {"pk": context.worker.pk}),
        )
    ]


@check("organization_for_plan")
def _organization_for_plan(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """第1-17号Ⅲ欄（自機関・事務所・支援責任者）に要る値（T31-D12・D13）。"""
    from services import form_data_1_17  # 循環を避ける

    issues = []
    settings_page = Destination("settings:organization_edit")
    organization = Organization.objects.first()
    if organization is None or not organization.name_kana:
        issues.append(
            Issue("organization_kana", _("The reading (kana) of our name is empty."), settings_page)
        )
    if organization is None or organization.support_start_planned_on is None:
        issues.append(
            Issue(
                "support_start_missing",
                _(
                    "The planned start date of support work (in the register of registered "
                    "support organizations) is empty."
                ),
                settings_page,
            )
        )
    office, managers = form_data_1_17.office_and_managers(context.worker)
    staff = context.staff
    if office is None:
        destination = (
            Destination("settings:staff_edit", {"pk": staff.pk})
            if staff is not None
            else Destination("workers:edit", {"pk": context.worker.pk})
        )
        issues.append(
            Issue(
                "office_missing",
                _("The assigned staff member has no office, so the support office is empty."),
                destination,
            )
        )
    if not managers and office is not None:
        issues.append(
            Issue(
                "manager_missing",
                _(
                    "The support manager of the support office (%(office)s) is not chosen. "
                    "Choose it on the office screen."
                )
                % {"office": office.name},
                Destination("settings:office_edit", {"pk": office.pk}),
            )
        )
    elif len(managers) > 1:
        issues.append(
            Issue(
                "managers_several",
                _(
                    "There are %(count)d support managers. Only one is printed; list all of "
                    "their names and positions in a separate document of any format."
                )
                % {"count": len(managers)},
                Destination("settings:staff"),
            )
        )
    return issues


@check("propriety_declared")
def _propriety_declared(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """Ⅲ欄７「支援の適正性を確保していることの有無」の申告（生成の画面。T31-D15）。"""
    if context.data.get("declaration.propriety"):
        return []
    return [
        Issue(
            "propriety_not_declared",
            _(
                "The declaration that the support is ensured to be proper (section III-7) is "
                "not made, so it is left blank."
            ),
            None,
        )
    ]


@check("text_fits")
def _text_fits(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """長い値が、字を下限まで小さくしても欄に収まらない（T31-D7）。"""
    from services import form_generation  # 循環を避ける

    if context.template is None:
        return []
    labels = {
        item["target"]: item.get("label", item["target"])
        for item in context.template.mapping.definition.get("fields", [])
    }
    return [
        Issue(
            "text_overflow",
            _('"%(label)s" does not fit in its field even at %(size)s pt.')
            % {"label": labels[fit.target], "size": f"{fit.size:g}"},
            None,
        )
        for fit in form_generation.fits(context.template, context.data)
        if fit.overflow
    ]


@check("delivery_warnings", params=("kind",))
def _delivery_warnings(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """その種別の取り消していない交付記録ごとの注意（T22）。"""
    kind = DeliveryKind(params["kind"])
    result: list[Issue] = []
    for delivery in deliveries.deliveries_of(context.worker).filter(
        kind=kind, voided_at__isnull=True
    ):
        destination = Destination(
            "workers:support_delivery", {"pk": context.worker.pk, "delivery_id": delivery.pk}
        )
        result.extend(
            Issue(warning.code, warning.message, destination, warning.strong)
            for warning in deliveries.warnings(delivery)
        )
    return result


# --- 雇用契約の書類（第1-4号・第1-5号・第1-6号。T34、ADR-0023）--------------------------------


def _version_destination(context: CheckContext, url_name: str) -> Destination | None:
    version = context.version
    if version is None:
        return None
    kwargs = {
        "pk": context.worker.pk,
        "contract_id": version.contract_id,
        "number": version.version,
    }
    return Destination(url_name, kwargs)


@check("contract_version_in_force")
def _contract_version_in_force(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """生成の対象の版が、基準日に適用される版でない（T34-D10）。"""
    version = context.version
    if version is None:
        return []
    in_force = contracts.version_on(version.contract, context.basis_on)
    if in_force is None or in_force.pk == version.pk:
        return []
    return [
        Issue(
            "version_not_in_force",
            _(
                "The version applied on %(day)s is version %(applied)d, not version %(number)d "
                "shown in the form."
            )
            % {
                "day": context.basis_on.isoformat(),
                "applied": in_force.version,
                "number": version.version,
            },
            Destination("workers:contracts", {"pk": context.worker.pk}),
        )
    ]


@check("contract_rows_fit")
def _contract_rows_fit(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """手当・控除・交代制が様式の行を超える（超えた行は書かない。合計は全部の行。T34-D7）。"""
    from services import form_data_contract  # 循環を避ける

    version = context.version
    if version is None:
        return []
    return [
        Issue(
            "rows_overflow",
            _(
                '"%(label)s" has %(count)d rows, but the form has %(limit)d. The rows after that '
                "are not printed (the totals include all rows); list them in a separate document."
            )
            % {"label": label, "count": count, "limit": limit},
            Destination("workers:contracts", {"pk": context.worker.pk}),
        )
        for label, count, limit in form_data_contract.overflowing_rows(version)
    ]


def _exact_hourly(version: EmploymentContractVersion) -> tuple[Fraction, str] | None:
    """1 時間当たりの金額（労働基準法施行規則 第19条第1項第4号・第2号。T34-D10、ADR-0023）。

    月給は月の所定労働時間数（版の値。月ごとに違えば 1 年間の 1 か月平均）で、日給は 1 日の
    所定労働時間数で割る。時間数が無ければ None。
    """
    if version.wage_type == WageType.MONTHLY and version.monthly_working_minutes:
        return Fraction(version.base_wage * 60, version.monthly_working_minutes), "monthly"
    if version.wage_type == WageType.DAILY and version.daily_working_minutes:
        return Fraction(version.base_wage * 60, version.daily_working_minutes), "daily"
    return None


@check("contract_wage_equivalent")
def _contract_wage_equivalent(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """月給・日給の「１時間当たりの金額」が、月給（日給）÷所定労働時間数と 1 円を超えて違う。"""
    version = context.version
    if version is None or version.wage_type == WageType.HOURLY:
        return []
    destination = Destination("workers:contracts", {"pk": context.worker.pk})
    exact = _exact_hourly(version)
    if exact is None:
        return [
            Issue(
                "working_hours_missing",
                _("The monthly working hours are empty, so the hourly amount cannot be checked."),
                destination,
            )
        ]
    amount, _basis = exact
    if version.hourly_equivalent is None:
        return [
            Issue(
                "hourly_equivalent_missing",
                _("The hourly amount (annex 1) is empty. It is %(amount)s yen by calculation.")
                % {"amount": f"{round(amount):,}"},
                destination,
            )
        ]
    if abs(version.hourly_equivalent - amount) <= 1:
        return []
    return [
        Issue(
            "hourly_equivalent_differs",
            _(
                "The hourly amount (%(entered)s yen) differs from the base wage divided by the "
                "scheduled working hours (%(amount)s yen; Ordinance for Enforcement of the Labor "
                "Standards Act, Article 19)."
            )
            % {"entered": f"{version.hourly_equivalent:,}", "amount": f"{round(amount):,}"},
            destination,
        )
    ]


@check("contract_monthly_hours_average")
def _contract_monthly_hours_average(
    context: CheckContext, params: Mapping[str, Any]
) -> list[Issue]:
    """月の所定労働時間数が、年の所定労働時間数の 1 か月平均（年÷12）と違う（第19条第1項第4号）。"""
    version = context.version
    if version is None or not (version.monthly_working_minutes and version.annual_working_minutes):
        return []
    average = Fraction(version.annual_working_minutes, 12)
    if abs(version.monthly_working_minutes - average) < 1:
        return []
    return [
        Issue(
            "monthly_hours_not_average",
            _(
                "The monthly working hours (%(monthly)s) differ from the annual working hours "
                "divided by 12 (%(average)s). When the hours differ by month, use the monthly "
                "average over a year."
            )
            % {
                "monthly": _hours(version.monthly_working_minutes),
                "average": _hours(round(average)),
            },
            Destination("workers:contracts", {"pk": context.worker.pk}),
        )
    ]


def _hours(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    return _("%(hours)d h %(minutes)02d min") % {"hours": hours, "minutes": rest}


@check("contract_contacts_split")
def _contract_contacts_split(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """窓口（Ⅸ４・５）が T34 より前の 1 つの文だけ（部署名の欄に全文を書く。T34-D4）。"""
    from services import form_data_contract  # 循環を避ける

    version = context.version
    if version is None or not form_data_contract.legacy_contacts(version):
        return []
    return [
        Issue(
            "contacts_not_split",
            _(
                "The contact points (IX 4 and 5) are entered as one text, so the whole text is "
                "printed in the department field. Enter the department, the person and the "
                "contact details separately in a new version."
            ),
            Destination("workers:contracts", {"pk": context.worker.pk}),
        )
    ]


@check("contract_conversion_annex")
def _contract_conversion_annex(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """無期転換後の条件が「変更あり」（別紙２ は生成しない。版に添付した別紙２ を使う。T34-D3）。"""
    version = context.version
    if version is None or version.conversion_conditions_change != ConversionChange.CHANGED:
        return []
    destination = Destination("workers:contracts", {"pk": context.worker.pk})
    if contracts.conversion_terms_missing(version):
        message = _(
            "The working conditions change after conversion, but annex 2 is not attached to the "
            "version. Annex 2 is not generated; attach it and use it with this form."
        )
    else:
        message = _(
            "Annex 2 (conditions after conversion) is not generated. Use the annex 2 attached "
            "to the version with this form."
        )
    return [Issue("conversion_annex", message, destination)]


@check("wage_explanation_complete")
def _wage_explanation_complete(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """報酬の説明（第1-4号）の比較の型と、その型で書く欄（「報酬根拠未入力」。T34-D10）。"""
    from services import wage_explanations  # 循環を避ける

    version = context.version
    if version is None:
        return []
    destination = _version_destination(context, "workers:contract_wage_explanation")
    return [
        Issue(missing.code, missing.message, destination)
        for missing in wage_explanations.missing_items(
            version, wage_explanations.explanation_of(version)
        )
    ]


@check("wage_equal_or_higher")
def _wage_equal_or_higher(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """日本人の報酬が申請人の報酬を上回る（基準省令 第1条第1項第3号。強い警告。T34-D10）。"""
    from services import wage_explanations  # 循環を避ける

    version = context.version
    if version is None:
        return []
    destination = _version_destination(context, "workers:contract_wage_explanation")
    return [
        Issue(missing.code, missing.message, destination, strong=True)
        for missing in wage_explanations.lower_than_japanese(
            version, wage_explanations.explanation_of(version)
        )
    ]


# --- 定期面談報告書（第5-5号・第5-6号。T35、ADR-0024）------------------------------------------


def _interview_destination(context: CheckContext) -> Destination | None:
    interview = context.interview
    if interview is None:
        return None
    return Destination(
        "workers:support_interview", {"pk": context.worker.pk, "interview_id": interview.pk}
    )


@check("interview_record")
def _interview_record(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """生成の対象が、取り消していない定期面談の組。"""
    interview = context.interview
    if interview is not None and interview.voided_at is None:
        return []
    return [
        Issue(
            "interview_missing",
            _("There is no record of the periodic interview."),
            Destination("workers:support_records", {"pk": context.worker.pk}),
        )
    ]


@check("interview_answers_present")
def _interview_answers_present(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """質問の回答が無い（T35 より前の記録）。質問・⑥・４ の欄は印刷済みのまま。"""
    from services import interview_answers  # 循環を避ける

    interview = context.interview
    if interview is None or interview_answers.has_answers(interview):
        return []
    return [
        Issue(
            "interview_answers_missing",
            _(
                "The interview has no answers to the questions (a record made before the "
                "questions were added). The questions, (6) and section 4 are left blank."
            ),
            _interview_destination(context),
        )
    ]


@check("interview_staff_role")
def _interview_staff_role(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """面談の対応者が支援責任者・支援担当者のどちらでもない（記載例 ★１）。"""
    interview = context.interview
    if interview is None:
        return []
    names = sorted(
        {
            record.staff.name
            for record in interview.records.select_related("staff")
            if not (record.staff.is_support_manager or record.staff.is_support_staff)
        }
    )
    if not names:
        return []
    return [
        Issue(
            "interview_staff_role",
            _(
                "%(names)s is neither a support manager nor a support staff member. The interview "
                "should be held by the support manager or staff in the support plan."
            )
            % {"names": "、".join(names)},
            Destination("settings:staff"),
        )
    ]


# --- キャリアアッププラン・実務経験証明書（自機関のテンプレート。T36、ADR-0025）------------------


@check("career_plan_exists")
def _career_plan_exists(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """契約の受入れ機関×分野（事業所）のキャリアアッププランが登録されている。"""
    from services import career_plans  # 循環を避ける

    contract = context.contract
    if contract is not None and career_plans.plan_for(contract, context.basis_on) is not None:
        return []
    host = context.host
    return [
        Issue(
            "career_plan_missing_content",
            _("No career-up plan is registered for the field (and site) of this contract."),
            Destination("hosts:detail", {"pk": host.pk}) if host else None,
        )
    ]


@check("career_plan_levels_fit")
def _career_plan_levels_fit(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """レベルが様式の行（5 行）を超える（超えた行は書かない）。"""
    from services import career_plans, form_data_career  # 循環を避ける

    contract = context.contract
    version = career_plans.plan_version_for(contract, context.basis_on) if contract else None
    if version is None or version.levels.count() <= form_data_career.LEVEL_ROWS:
        return []
    return [
        Issue(
            "career_plan_levels_overflow",
            _(
                "The plan has %(count)d levels, but the form has %(limit)d rows. The rows after "
                "that are not printed; list them in a separate document."
            )
            % {"count": version.levels.count(), "limit": form_data_career.LEVEL_ROWS},
            Destination(
                "hosts:career_plan",
                {"pk": version.plan.host_organization_id, "plan_id": version.plan_id},
            ),
        )
    ]


@check("career_plan_transfer_scope")
def _career_plan_transfer_scope(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """水産加工業の区分の契約で、プランの「転職できる範囲」が空（協議会の定め。T36-D5）。"""
    from services import career_plans  # 循環を避ける

    contract = context.contract
    if contract is None or not career_plans.is_fishery(context.version):
        return []
    version = career_plans.plan_version_for(contract, context.basis_on)
    if version is None or version.transfer_scope.strip():
        return []
    return [
        Issue(
            "transfer_scope_missing",
            _(
                "The contract is in the seafood processing category, but the plan does not "
                "explain the scope of job changes. The council requires explaining it and "
                "obtaining consent."
            ),
            Destination(
                "hosts:career_plan",
                {"pk": version.plan.host_organization_id, "plan_id": version.plan_id},
            ),
        )
    ]


@check("experience_period_started")
def _experience_period_started(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """実務経験証明書: 契約の期間が基準日に始まっている。"""
    from services import form_data_career  # 循環を避ける

    contract = context.contract
    if contract is None or form_data_career.period_start(contract) <= context.basis_on:
        return []
    return [
        Issue(
            "experience_not_started",
            _("The contract has not started on %(day)s, so there is no experience to certify.")
            % {"day": context.basis_on.isoformat()},
            Destination("workers:contracts", {"pk": context.worker.pk}),
        )
    ]


# --- 受入れ機関・分野の書類（第3-1号・第13-1号・第13-2号・第5-10号・第1-25号。T37、ADR-0026）
# 誓約書の固定の文が述べる事実と記録が食い違えば生成不可、固定の文が述べない事実は警告（ADR-0021 の
# 考え方。T37-D5）。


@check("contract_field_is", params=("field",))
def _contract_field_is(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """契約の事業所の分野が、誓約書の分野（誓約事項の業務）である。"""
    site = context.site
    if site is None or site.field.code == params["field"]:
        return []
    return [
        Issue(
            "field_differs",
            _("The site of the contract is in another field (%(field)s).")
            % {"field": site.field.name},
            Destination("workers:contracts", {"pk": context.worker.pk}),
        )
    ]


@check("categories_in_field")
def _categories_in_field(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """契約の業務区分が、事業所の分野の業務区分で、基準日に有効（誓約事項の業務）。"""
    version, site = context.version, context.site
    if version is None or site is None:
        return []
    on = context.basis_on
    bad = [
        category.name
        for category in version.categories.all()
        if category.field_id != site.field_id
        or (category.valid_from is not None and category.valid_from > on)
        or (category.valid_until is not None and category.valid_until < on)
    ]
    if not bad:
        return []
    return [
        Issue(
            "category_not_pledged",
            _("These operation categories are not the duties pledged in the form: %(names)s")
            % {"names": "、".join(bad)},
            Destination("workers:contracts", {"pk": context.worker.pk}),
        )
    ]


@check("industry_listed", params=("list", "mode"))
def _industry_listed(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """事業所の日本標準産業分類が誓約書の産業の一覧にある。

    mode="none" は不一致を、"limited" は限定つきの項目・粗いコードを出す。
    """
    from services import industries  # 循環を避ける

    site, host = context.site, context.host
    if site is None or host is None:
        return []
    found = industries.match(params["list"], site.industry_code)
    destination = Destination("hosts:detail", {"pk": host.pk})
    names = {"site": site.name, "code": site.industry_code}
    if params.get("mode") == "none" and found.result == "none":
        return [
            Issue(
                "industry_not_listed",
                _(
                    "The industry code of %(site)s (%(code)s) is not in the industries pledged "
                    "in the form."
                )
                % names,
                destination,
            )
        ]
    if params.get("mode") == "limited" and found.result in ("limited", "coarse") and found.item:
        if found.result == "limited":
            message = _(
                "The industry of %(site)s (%(code)s) is pledged only with a limitation: "
                "%(item)s. Check that the site meets it."
            ) % (names | {"item": found.item["name"]})
        else:
            message = _(
                "The industry code of %(site)s (%(code)s) is too coarse to compare with "
                "%(item)s. Enter the detailed code."
            ) % (names | {"item": found.item["name"]})
        return [Issue("industry_limited", message, destination)]
    return []


def _own_council_certificate_missing() -> bool:
    organization = Organization.objects.first()
    if organization is None or organization.food_council_joined_on is None:
        return True
    return not organization.attachments.filter(
        kind=AttachmentKind.FOOD_COUNCIL_CERTIFICATE, voided_at__isnull=True
    ).exists()


@check("own_council_certificate", params=("only_full_support",))
def _own_council_certificate(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """自機関（登録支援機関）の協議会の加入と構成員証明書（T37-D5）。"""
    if params.get("only_full_support"):
        host = context.host
        support = hosts.active_contract(host, context.basis_on) if host else None
        if support is None or support.scope != SupportScope.FULL:
            return []
    if not _own_council_certificate_missing():
        return []
    return [
        Issue(
            "own_certificate_missing",
            _(
                "Our membership of the Food Industry SSW Council (date joined and certificate) "
                "is not registered."
            ),
            Destination("settings:organization_edit"),
        )
    ]


@check("support_contract_terms", params=("form",))
def _support_contract_terms(context: CheckContext, params: Mapping[str, Any]) -> list[Issue]:
    """支援委託契約の書類の注意（全部委託でない・終了日が空・内訳が無い・管轄裁判所が空。T37-D12）。"""
    host = context.host
    support = hosts.active_contract(host, context.basis_on) if host else None
    if host is None or support is None:
        return []
    destination = Destination("hosts:detail", {"pk": host.pk})
    issues = []
    if support.scope != SupportScope.FULL:
        issues.append(
            Issue(
                "not_full_support",
                _("The support contract is not a full entrustment."),
                destination,
            )
        )
    if params.get("form") in ("5-10", "1-25") and support.ends_on is None:
        issues.append(
            Issue(
                "support_contract_no_end",
                _("The support contract has no end date, so the end of the period is left blank."),
                destination,
            )
        )
    if params.get("form") == "5-10":
        if not support.fees.exists():
            issues.append(
                Issue(
                    "fee_breakdown_missing",
                    _("The breakdown of the fee (annex) is not entered, so it is left blank."),
                    Destination(
                        "hosts:support_contract_fees", {"pk": host.pk, "contract_id": support.pk}
                    ),
                )
            )
        organization = Organization.objects.first()
        if organization is None or not organization.court_name.strip():
            issues.append(
                Issue(
                    "court_missing",
                    _("The district court of Article 8 is not set, so it is left blank."),
                    Destination("settings:organization_edit"),
                )
            )
    if params.get("form") == "1-25":
        from services import form_data_application  # 循環を避ける

        organization = Organization.objects.first()
        number = organization.registration_number if organization else ""
        if not form_data_application.REGISTRATION.match(number):
            issues.append(
                Issue(
                    "registration_number_format",
                    _(
                        "Our registration number (%(number)s) is not in the form "
                        '"00登-000000", so it is written as it is.'
                    )
                    % {"number": number or "—"},
                    Destination("settings:organization_edit"),
                )
            )
    return issues


# --- データの規則 ---------------------------------------------------------------------


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _is_date(value: Any) -> bool:
    if isinstance(value, datetime.date):
        return True
    try:
        datetime.date.fromisoformat(str(value))
    except ValueError:
        return False
    return True


def _width_ok(value: Any, width: str) -> bool:
    """半角（Na・H）だけ／全角（F・W・A）だけか（unicodedata.east_asian_width）。"""
    allowed = ("Na", "H") if width == "half" else ("F", "W", "A")
    return all(unicodedata.east_asian_width(char) in allowed for char in str(value))


def _field_issue(rule: Mapping[str, Any], value: Any) -> str | None:
    """データの規則の不足の文言（不足が無ければ None）。空の値は required だけが見る。"""
    label = rule["label"]
    kind = rule["type"]
    if kind == "required":
        return _('"%(label)s" is empty.') % {"label": label} if _is_empty(value) else None
    if _is_empty(value):
        return None
    if kind == "date" and not _is_date(value):
        return _('"%(label)s" is not a date.') % {"label": label}
    if kind == "width" and not _width_ok(value, rule["width"]):
        if rule["width"] == "half":
            return _('"%(label)s" must be in half-width characters.') % {"label": label}
        return _('"%(label)s" must be in full-width characters.') % {"label": label}
    if kind == "max_length" and len(str(value)) > rule["max_length"]:
        return _('"%(label)s" is longer than %(limit)d characters.') % {
            "label": label,
            "limit": rule["max_length"],
        }
    return None


# --- 評価 -------------------------------------------------------------------------


def evaluate(rules: Sequence[Mapping[str, Any]], context: CheckContext) -> CheckResult:
    """規則を上から順に評価する（requires の規則で不足が出たら、その規則は飛ばす）。"""
    failed: set[str] = set()
    blocking: list[Finding] = []
    warnings: list[Finding] = []
    for rule in rules:
        if rule.get("requires") in failed:
            continue
        issues = _issues(rule, context)
        if issues:
            failed.add(rule["id"])
        for issue in issues:
            finding = Finding(
                rule["id"],
                issue.code,
                rule["severity"],
                issue.message,
                issue.destination,
                issue.strong,
            )
            (blocking if rule["severity"] == BLOCK else warnings).append(finding)
    return CheckResult(blocking, warnings)


def _issues(rule: Mapping[str, Any], context: CheckContext) -> list[Issue]:
    if rule["type"] == "check":
        registered = CHECKS[rule["name"]]
        return registered.function(context, rule.get("params", {}))
    message = _field_issue(rule, context.data.get(rule["key"]))
    if message is None:
        return []
    fix = rule.get("fix")
    destination = None
    if fix:
        kwargs = _object_kwargs(context, fix["object"])
        destination = Destination(fix["url"], kwargs) if kwargs is not None else None
    return [Issue(rule["type"], message, destination)]


# --- 規則のデータの検査（読み込みで使う）-------------------------------------------------


def validate_rules(rules: Any, field_keys: set[str]) -> None:
    """規則のデータが評価できる形か（不備は RulesError）。"""
    if not isinstance(rules, list):
        raise RulesError("rules must be a list")
    seen: set[str] = set()
    for index, rule in enumerate(rules):
        where = f"rules[{index}]"
        if not isinstance(rule, dict) or not isinstance(rule.get("id"), str) or not rule["id"]:
            raise RulesError(f"{where}: id is required")
        where = f"rule {rule['id']}"
        if rule["id"] in seen:
            raise RulesError(f"{where}: duplicate id")
        if rule.get("type") not in RULE_TYPES:
            raise RulesError(f"{where}: unknown type {rule.get('type')!r}")
        if rule.get("severity") not in SEVERITIES:
            raise RulesError(f"{where}: severity must be block or warn")
        if "requires" in rule and rule["requires"] not in seen:
            raise RulesError(f"{where}: requires must name an earlier rule")
        if rule["type"] == "check":
            _validate_check_rule(rule, where)
        else:
            _validate_field_rule(rule, where, field_keys)
        seen.add(rule["id"])


def _validate_check_rule(rule: Mapping[str, Any], where: str) -> None:
    registered = CHECKS.get(rule.get("name", ""))
    if registered is None:
        raise RulesError(f"{where}: unknown check {rule.get('name')!r}")
    params = rule.get("params", {})
    if not isinstance(params, dict) or set(params) - set(registered.params):
        raise RulesError(f"{where}: unknown params {sorted(set(params) - set(registered.params))}")


def _validate_field_rule(rule: Mapping[str, Any], where: str, field_keys: set[str]) -> None:
    if rule.get("key") not in field_keys:
        raise RulesError(f"{where}: key {rule.get('key')!r} is not in the fields of this version")
    if not isinstance(rule.get("label"), str) or not rule["label"]:
        raise RulesError(f"{where}: label is required")
    if rule["type"] == "width" and rule.get("width") not in ("half", "full"):
        raise RulesError(f"{where}: width must be half or full")
    if rule["type"] == "max_length" and not (
        isinstance(rule.get("max_length"), int) and rule["max_length"] > 0
    ):
        raise RulesError(f"{where}: max_length must be a positive integer")
    fix = rule.get("fix")
    if fix is None:
        return
    if not isinstance(fix, dict) or fix.get("object") not in FIX_OBJECT_KWARGS:
        raise RulesError(f"{where}: fix.object must be one of {sorted(FIX_OBJECT_KWARGS)}")
    try:
        reverse(fix.get("url", ""), kwargs=FIX_OBJECT_KWARGS[fix["object"]])
    except NoReverseMatch as error:
        raise RulesError(f"{where}: fix.url {fix.get('url')!r} does not resolve") from error
