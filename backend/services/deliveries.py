"""本人への書類の交付記録（第4章・第6章「署名を要する書類の扱い」、T22、ADR-0010）。

- 種別: キャリアアッププラン（飲食料品製造業分野）／実務経験証明書（両分野、求めに応じて）／
  支援計画書の写し／雇用契約書・雇用条件書の写し／その他。
- 上書きしない。誤りは取消（理由必須）＋新しい行。後から足せるのは署名済みの控え（添付）だけ。
- キャリアアッププランの未交付は雇用契約ごとに判定する（転籍前の契約の交付記録は数えない）。
  対象は事業所の分野が飲食料品製造業分野の契約（業務区分では判定しない）。
- 署名済みの控えの有無は「取り消していない添付があるか」（has_receipt。一覧と詳細で同じ）。
- 警告は保存を止めない。
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError
from django.core.files import File
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.contracts.models import EmploymentContract
from apps.core.models import Attachment, AttachmentKind, SupportLanguage, User
from apps.forms_engine.models import GeneratedDocument, GenerationStatus
from apps.support.models import (
    DeliveredBy,
    DeliveryKind,
    DeliveryMethod,
    DocumentDelivery,
    SupportPlanVersion,
)
from apps.workers.models import ForeignWorker
from services import attachments, support_records, workers

PLAN_FORM_ID = "1-17"  # 支援計画書の写しの元にできる様式
# 雇用契約を対象に生成した自機関の書類（T36）。交付記録の種別ごとに元にできる様式
CONTRACT_FORM_IDS = {
    DeliveryKind.CAREER_UP_PLAN: "career-plan",
    DeliveryKind.EXPERIENCE_CERTIFICATE: "experience-certificate",
}

FOOD_FIELD = "food_manufacturing"  # 飲食料品製造業分野（2027-04-01 以降の水産加工業の区分も含む）
RECEIPT_KIND = AttachmentKind.DELIVERED_DOCUMENT

# 当該契約が必須の種別（契約前の交付・当該契約に係る実務経験・契約の書類）
CONTRACT_KINDS = frozenset(
    {
        DeliveryKind.CAREER_UP_PLAN,
        DeliveryKind.EXPERIENCE_CERTIFICATE,
        DeliveryKind.EMPLOYMENT_CONTRACT_COPY,
    }
)
# 内容の説明が要る種別（キャリアアッププラン: 告示、支援計画書: 特定技能基準省令・運用要領）
EXPLANATION_KINDS = frozenset({DeliveryKind.CAREER_UP_PLAN, DeliveryKind.SUPPORT_PLAN_COPY})
# 署名済みの控えが要る種別（第6章の署名を要する書類）
RECEIPT_KINDS = frozenset(
    {
        DeliveryKind.CAREER_UP_PLAN,
        DeliveryKind.SUPPORT_PLAN_COPY,
        DeliveryKind.EMPLOYMENT_CONTRACT_COPY,
    }
)


@dataclass
class DeliveryInput:
    kind: str
    delivered_on: datetime.date
    method: str
    delivered_by: str
    title: str = ""
    method_note: str = ""
    delivered_by_name: str = ""
    explained: bool = False
    contract: EmploymentContract | None = None
    requested_on: datetime.date | None = None
    languages: Sequence[SupportLanguage] = ()
    note: str = ""
    # 生成履歴から作るときの元の生成物（同じ外国人の第1-17号。T31-D16。自機関の書類は T36）
    generated_document: GeneratedDocument | None = None
    # 水産加工業の区分のキャリアアッププラン: 転職できる範囲を含むキャリアパスの説明に同意を得た
    consented: bool = False


class _Errors:
    def __init__(self) -> None:
        self.items: dict[str, list[ValidationError]] = {}

    def add(self, key: str, message: str, code: str) -> None:
        self.items.setdefault(key, []).append(ValidationError(message, code=code))

    def raise_if_any(self) -> None:
        if self.items:
            raise ValidationError(self.items)


def validate(
    worker: ForeignWorker, data: DeliveryInput, *, today: datetime.date
) -> SupportPlanVersion | None:
    """入力を検査し、支援計画書の写しなら交付日に適用されていた計画の版を返す。

    支援計画書の写しは、交付日に適用されていた版が無ければ保存できない（写しの対象が特定できない）。
    """
    errors = _Errors()
    if not workers.is_active(worker):
        errors.add("__all__", _("This foreign worker is deactivated."), "deactivated")
    if data.kind not in DeliveryKind.values:
        errors.add("kind", _("Select a valid choice."), "invalid_choice")
    elif data.kind == DeliveryKind.OTHER and not data.title.strip():
        errors.add("title", _("Enter the name of the document."), "required")
    if data.delivered_on > today:
        errors.add("delivered_on", _("The date cannot be in the future."), "future")
    if data.method not in DeliveryMethod.values:
        errors.add("method", _("Select a valid choice."), "invalid_choice")
    elif data.method == DeliveryMethod.OTHER and not data.method_note.strip():
        errors.add("method_note", _("Enter how it was delivered."), "required")
    if data.delivered_by not in DeliveredBy.values:
        errors.add("delivered_by", _("Select a valid choice."), "invalid_choice")
    if data.contract is None:
        if data.kind in CONTRACT_KINDS:
            errors.add("contract", _("Select the employment contract."), "required")
    elif data.contract.worker_id != worker.pk:
        errors.add("contract", _("Select a contract of this foreign worker."), "other_worker")
    if data.requested_on is not None:
        if data.kind != DeliveryKind.EXPERIENCE_CERTIFICATE:
            errors.add(
                "requested_on",
                _("Enter this only for a certificate of work experience."),
                "requested_on",
            )
        elif data.requested_on > data.delivered_on:
            errors.add(
                "requested_on",
                _("The date of the request cannot be after the delivery."),
                "requested_after",
            )
    if data.consented and data.kind != DeliveryKind.CAREER_UP_PLAN:
        errors.add("consented", _("Only for a career-up plan."), "consented")
    plan_version = None
    if data.generated_document is not None and data.kind in CONTRACT_FORM_IDS:
        _check_contract_document(errors, worker, data)
    elif data.generated_document is not None:
        plan_version = _generated_plan_version(errors, worker, data)
    elif data.kind == DeliveryKind.SUPPORT_PLAN_COPY and "delivered_on" not in errors.items:
        plan_version = support_records.plan_version_for(worker, data.delivered_on)
        if plan_version is None:
            errors.add(
                "delivered_on",
                _(
                    "There is no support plan in effect on this date, so the copy cannot be "
                    "identified. Create the support plan or check the date."
                ),
                "no_plan",
            )
    errors.raise_if_any()
    return plan_version


def _generated_plan_version(
    errors: _Errors, worker: ForeignWorker, data: DeliveryInput
) -> SupportPlanVersion | None:
    """生成物から作る写しの計画の版（生成物の版。交付日に適用される版と違ってもよい。T31-D16）。

    元にできるのは、同じ外国人の第1-17号の、完了した生成物だけ。
    """
    document = data.generated_document
    assert document is not None
    target = document.target
    if (
        data.kind != DeliveryKind.SUPPORT_PLAN_COPY
        or document.worker_id != worker.pk
        or document.template.form_id != PLAN_FORM_ID
        or document.status != GenerationStatus.COMPLETED
        or not isinstance(target, SupportPlanVersion)
    ):
        errors.add(
            "__all__",
            _("Only a completed support plan (form 1-17) of this foreign worker can be used."),
            "generated_document",
        )
        return None
    return target


def _check_contract_document(errors: _Errors, worker: ForeignWorker, data: DeliveryInput) -> None:
    """自機関の書類（キャリアアッププラン・実務経験証明書）の生成物から作る交付記録（T36）。

    元にできるのは、同じ外国人の、その種別の様式の完了した生成物で、対象が交付記録の契約のもの。
    """
    document = data.generated_document
    assert document is not None
    target = document.target
    if (
        document.worker_id != worker.pk
        or document.template.form_id != CONTRACT_FORM_IDS[DeliveryKind(data.kind)]
        or document.status != GenerationStatus.COMPLETED
        or not isinstance(target, EmploymentContract)
        or (data.contract is not None and target.pk != data.contract.pk)
    ):
        errors.add(
            "__all__",
            _("Only a completed document of this kind for the selected contract can be used."),
            "generated_document",
        )


def validate_files(files: Iterable[File[Any]]) -> list[File[Any]]:
    files = list(files)
    for file in files:
        try:
            attachments.validate_upload(RECEIPT_KIND, file)
        except ValidationError as error:
            raise ValidationError({"files": error.messages}) from error
    return files


def create_delivery(
    worker: ForeignWorker,
    data: DeliveryInput,
    *,
    by: User | None,
    files: Iterable[File[Any]] = (),
    today: datetime.date | None = None,
) -> DocumentDelivery:
    """交付記録を作る（支援計画書の写しは、交付日に適用されていた支援計画の版を紐づける）。"""
    today = today or timezone.localdate()
    plan_version = validate(worker, data, today=today)
    uploads = validate_files(files)
    with transaction.atomic():
        delivery = DocumentDelivery(
            worker=worker,
            kind=data.kind,
            title=data.title.strip(),
            delivered_on=data.delivered_on,
            method=data.method,
            method_note=data.method_note.strip() if data.method == DeliveryMethod.OTHER else "",
            delivered_by=data.delivered_by,
            delivered_by_name=data.delivered_by_name.strip(),
            explained=data.explained,
            consented=data.consented,
            contract=data.contract,
            requested_on=data.requested_on,
            plan_version=plan_version,
            generated_document=data.generated_document,
            note=data.note.strip(),
            created_by=by,
            updated_by=by,
        )
        delivery.save()
        if data.kind == DeliveryKind.CAREER_UP_PLAN and data.contract is not None:
            # 交付したキャリアアッププランの版（交付日に適用）は、これ以後は直せない
            # （修正 1 回目 J1）
            from services import career_plans  # 循環を避ける

            career_plans.mark_used(career_plans.plan_version_for(data.contract, data.delivered_on))
        delivery.languages.set(data.languages)
        for file in uploads:
            attachments.create_attachment(
                target=delivery, kind=RECEIPT_KIND, file=file, created_by=by
            )
    return delivery


def add_receipts(
    delivery: DocumentDelivery, files: Iterable[File[Any]], *, by: User | None
) -> list[Attachment]:
    """署名済みの控え・受領の記録を足す（取り消した記録・無効化した外国人には足さない）。"""
    if delivery.voided_at is not None or not workers.is_active(delivery.worker):
        raise ValidationError(_("Attachments cannot be added to this record."), code="locked")
    uploads = validate_files(files)
    if not uploads:
        raise ValidationError({"files": [_("Select a file.")]})
    with transaction.atomic():
        return [
            attachments.create_attachment(
                target=delivery, kind=RECEIPT_KIND, file=file, created_by=by
            )
            for file in uploads
        ]


def void_delivery(delivery: DocumentDelivery, *, by: User | None, reason: str) -> None:
    """取り消す（打ち消して残し、交付の判定から外す）。正しい記録は新しく足す。"""
    if delivery.voided_at is not None:
        raise ValidationError(_("This record is already voided."), code="voided")
    if not reason.strip():
        raise ValidationError(_("Enter the reason for voiding."), code="void_reason")
    if not workers.is_active(delivery.worker):
        raise ValidationError(_("This foreign worker is deactivated."), code="deactivated")
    delivery.voided_at = timezone.now()
    delivery.voided_by = by
    delivery.void_reason = reason.strip()
    delivery.updated_by = by
    delivery.save(
        update_fields=["voided_at", "voided_by", "void_reason", "updated_by", "updated_at"]
    )


# --- 一覧・判定 -------------------------------------------------------------------------


def deliveries_of(worker: ForeignWorker) -> models.QuerySet[DocumentDelivery]:
    """外国人の交付記録（取り消した記録も含む。新しい順）。"""
    return (
        DocumentDelivery.objects.filter(worker=worker)
        .select_related("contract__host_organization", "plan_version", "voided_by")
        .prefetch_related("languages", "attachments")
        .order_by("-delivered_on", "-pk")
    )


def has_receipt(
    delivery: DocumentDelivery, delivery_attachments: Iterable[Attachment] | None = None
) -> bool:
    """署名済みの控えがある: 取り消していない添付がある（一覧と詳細で同じ判定）。

    delivery_attachments に（一覧で prefetch した）添付を渡すと、それを見る。
    """
    files = delivery.attachments.all() if delivery_attachments is None else delivery_attachments
    return any(file.kind == RECEIPT_KIND and file.voided_at is None for file in files)


def is_food_contract(contract: EmploymentContract) -> bool:
    """いずれかの版の就業の場所の分野が飲食料品製造業分野（業務区分では判定しない）。"""
    return contract.versions.filter(site__field__code=FOOD_FIELD).exists()


def signed_on(contract: EmploymentContract) -> datetime.date | None:
    """契約の締結日（第1版の締結日）。"""
    first = contract.versions.order_by("version").first()
    return first.signed_on if first is not None else None


@dataclass(frozen=True)
class DeliveryWarning:
    code: str
    message: str
    strong: bool = False


def _timing_warning(
    delivered_on: datetime.date, contract: EmploymentContract
) -> DeliveryWarning | None:
    """キャリアアッププランの交付が契約の前か（締結日と同じ日は警告しない）。"""
    concluded = signed_on(contract)
    if concluded is None:
        return DeliveryWarning(
            "no_signed_on",
            _(
                "The contract has no date of conclusion, so it cannot be checked that the plan "
                "was given before the contract."
            ),
        )
    if delivered_on > concluded:
        return DeliveryWarning(
            "after_signing",
            _("Given after the employment contract was concluded (%(day)s).")
            % {"day": concluded.isoformat()},
            strong=True,
        )
    return None


def warnings(
    delivery: DocumentDelivery, delivery_attachments: Iterable[Attachment] | None = None
) -> list[DeliveryWarning]:
    """記録 1 件の注意（取り消した記録には出さない）。"""
    if delivery.voided_at is not None:
        return []
    result: list[DeliveryWarning] = []
    kind = delivery.kind
    if (
        kind == DeliveryKind.CAREER_UP_PLAN
        and delivery.contract is not None
        and is_food_contract(delivery.contract)
    ):
        timing = _timing_warning(delivery.delivered_on, delivery.contract)
        if timing is not None:
            result.append(timing)
    if delivery.generated_document_id is not None and delivery.plan_version is not None:
        applied = support_records.plan_version_for(delivery.worker, delivery.delivered_on)
        if applied is None or applied.pk != delivery.plan_version_id:
            result.append(
                DeliveryWarning(
                    "version_differs",
                    _(
                        "This is a copy of version %(version)s, but the version in effect on the "
                        "delivery date is %(applied)s."
                    )
                    % {
                        "version": delivery.plan_version.version,
                        "applied": applied.version if applied else _("none"),
                    },
                )
            )
    if (
        kind == DeliveryKind.CAREER_UP_PLAN
        and delivery.contract is not None
        and not delivery.consented
        and _fishery_contract(delivery.contract, delivery.delivered_on)
    ):
        result.append(
            DeliveryWarning(
                "no_consent",
                _(
                    "The contract is in the seafood processing category. Record that the worker "
                    "consented to the career path including the scope of job changes."
                ),
            )
        )
    if kind in EXPLANATION_KINDS and not delivery.explained:
        result.append(DeliveryWarning("not_explained", _("There is no record of explaining it.")))
    if kind in RECEIPT_KINDS and not has_receipt(delivery, delivery_attachments):
        result.append(
            DeliveryWarning("no_receipt", _("The signed copy or receipt is not attached."))
        )
    return result


def _fishery_contract(contract: EmploymentContract, on: datetime.date) -> bool:
    """契約の版（交付日。無ければ最初の版）が水産加工業の区分か（2027-04-01 から。T36-D5）。"""
    from services import career_plans, contracts  # 循環を避ける

    return career_plans.is_fishery(contracts.version_on(contract, on))


@dataclass(frozen=True)
class ContractFinding:
    """飲食料品製造業分野の契約 1 つの、キャリアアッププランの交付の注意。"""

    contract: EmploymentContract
    warning: DeliveryWarning


def career_plan_findings(worker: ForeignWorker) -> list[ContractFinding]:
    """飲食料品製造業分野の契約ごとに、キャリアアッププランの交付を判定する。

    その契約に紐づく取り消していない交付記録が無ければ「未交付」。あれば、最も早い交付が
    契約の前か（締結日と比べる）。前の契約（転籍前）の交付記録は数えない。
    段階3の生成の検査（仕様書 第4章「未交付の場合は警告」）でも使う。
    """
    contracts = (
        EmploymentContract.objects.filter(worker=worker)
        .select_related("host_organization")
        .order_by("current_starts_on", "pk")
    )
    result = []
    for contract in contracts:
        if not is_food_contract(contract):
            continue
        first = (
            DocumentDelivery.objects.filter(
                contract=contract, kind=DeliveryKind.CAREER_UP_PLAN, voided_at__isnull=True
            )
            .order_by("delivered_on", "pk")
            .first()
        )
        if first is None:
            result.append(
                ContractFinding(
                    contract,
                    DeliveryWarning(
                        "career_plan_missing",
                        _(
                            "No career-up plan has been recorded as given for this contract "
                            "(food and beverage manufacturing)."
                        ),
                        strong=True,
                    ),
                )
            )
            continue
        timing = _timing_warning(first.delivered_on, contract)
        if timing is not None:
            result.append(ContractFinding(contract, timing))
    return result
