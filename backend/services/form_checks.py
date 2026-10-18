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
from functools import cached_property
from typing import Any

from django.db import models
from django.urls import NoReverseMatch, reverse
from django.utils.translation import gettext as _

from apps.contracts.models import EmploymentContract, EmploymentContractVersion
from apps.core.models import AttachmentKind, Organization, Staff
from apps.hosts.models import HostOrganization, HostSite
from apps.support.models import Briefing, DeliveryKind, SupportPlanVersion
from apps.workers.models import ApplicationType, ForeignWorker, LatestResidenceRecord
from services import briefings, contracts, deliveries, hosts
from services.support_catalog import Method, SupportItem

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
        return contracts.contract_on(self.worker, self.basis_on)

    @cached_property
    def version(self) -> EmploymentContractVersion | None:
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
    raise RulesError(f"unknown fix object: {name}")


FIX_OBJECT_KWARGS: dict[str, dict[str, int]] = {
    "worker": {"pk": 1},
    "briefing": {"pk": 1, "briefing_id": 1},
    "host": {"pk": 1},
    "staff": {"pk": 1},
    "organization": {},
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
            % {"codes": "、".join(codes)},
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
    if not managers:
        issues.append(
            Issue(
                "manager_missing",
                _("There is no support manager for the support office."),
                Destination("settings:staff"),
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
