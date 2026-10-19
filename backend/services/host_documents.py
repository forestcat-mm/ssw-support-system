"""受入れ機関・事業所の書類の保管庫（T42 の追加、ADR-0033 の追記）。

- 書類ごとに種類・対象の事業所（任意）・対象期間または発行日・有効期限・言語を持つ。ファイルは添付。
- 申請の書類の一覧と統合 PDF では、雇用契約の事業所と申請の基準日から、有効なものを自動で選ぶ
  （有効なものが無ければ最新のものを選んで警告）。行との対応は書類の名前の語で決める（第2表の書類は
  第2表の行だけ。申請人の住民税の行と混ざらないように）。
- 警告: 対象期間の外・有効期限の後、発行後 3 か月を超える（第2表の原本。表紙の留意事項２。基準日から
  遡って 3 か月。T42-D27）、発行日が無い、年間カレンダーが日本語と申請人の支援言語の併記でない。
  既に受け入れている機関の第2表の書類は、統合 PDF の画面で注意（submission_bundles）。
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Mapping
from typing import Any

from django.core.exceptions import ValidationError
from django.core.files import File
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.core.models import Attachment, AttachmentKind, User
from apps.hosts.models import HostDocument, HostDocumentKind, HostOrganization, HostSite
from apps.workers.models import ForeignWorker
from services import attachments, deadlines

K = HostDocumentKind
# 種類 → (申請の書類の一覧の行の書類の名前に含まれる語, 第2表の行だけか)
KIND_ROWS: dict[str, tuple[tuple[str, ...], bool]] = {
    K.VARIABLE_HOURS_AGREEMENT: (("変形労働時間制に関する協定書",), False),
    K.ANNUAL_CALENDAR: (("年間カレンダー",), False),
    K.WAGE_RULES: (("賃金規程",), False),
    K.REGISTRY_CERTIFICATE: (("登記事項証明書",), True),
    K.OFFICER_RESIDENCE: (("役員の住民票",), True),
    K.OWNER_RESIDENCE: (("個人事業主の住民票",), True),
    K.LABOR_INSURANCE: (("労働保険料等納付証明書",), True),
    K.SOCIAL_INSURANCE: (("社会保険料納入状況回答票",), True),
    K.NATIONAL_TAX: (("納税証明書（その３）",), True),
    K.LOCAL_TAX: (("住民税の納税証明書",), True),
}
PERIOD_KINDS = frozenset({K.VARIABLE_HOURS_AGREEMENT, K.ANNUAL_CALENDAR})
SITE_KINDS = PERIOD_KINDS  # 事業所ごとの書類
ISSUED_KINDS = frozenset(
    {
        K.REGISTRY_CERTIFICATE,
        K.OFFICER_RESIDENCE,
        K.OWNER_RESIDENCE,
        K.LABOR_INSURANCE,
        K.SOCIAL_INSURANCE,
        K.NATIONAL_TAX,
        K.LOCAL_TAX,
    }
)
ISSUED_MONTHS = 3  # 発行後 3 か月以内（表紙の留意事項２）
ENDING_DAYS = 30  # 受入れ機関の書類の一覧で「終了が近い」と出す日数（T42-D26）
JAPANESE = "ja"
TABLE2 = "_t2_"


class Issue:
    OUT_OF_PERIOD = "out_of_period"
    ISSUED_LONG_AGO = "issued_long_ago"
    ISSUED_ON_MISSING = "issued_on_missing"
    CALENDAR_LANGUAGE = "calendar_language"
    TABLE2_NOT_NEEDED = "table2_not_needed"


ISSUE_TEXTS: Mapping[str, Any] = {
    Issue.OUT_OF_PERIOD: gettext_lazy(
        "The basis date is outside the period or after the expiry date of the document."
    ),
    Issue.ISSUED_LONG_AGO: gettext_lazy(
        "The document was issued more than 3 months before the basis date (originals must be "
        "issued within 3 months)."
    ),
    Issue.ISSUED_ON_MISSING: gettext_lazy("The issue date of the document is not entered."),
    Issue.CALENDAR_LANGUAGE: gettext_lazy(
        "The annual calendar is not written in both Japanese and the worker's support language."
    ),
    Issue.TABLE2_NOT_NEEDED: gettext_lazy(
        "The host organization already accepts specified skilled workers this fiscal year, so "
        "the documents of table 2 are not required."
    ),
}


def issue_text(code: str) -> str:
    return str(ISSUE_TEXTS[code])


def is_table2(row: Mapping[str, Any]) -> bool:
    return TABLE2 in str(row.get("check", {}).get("file", ""))


def kinds_for_row(row: Mapping[str, Any]) -> list[str]:
    document = str(row.get("document", ""))
    return [
        kind
        for kind, (words, table2_only) in KIND_ROWS.items()
        if any(word in document for word in words) and (is_table2(row) or not table2_only)
    ]


def live(host: HostOrganization) -> list[HostDocument]:
    return list(
        host.documents.filter(voided_at__isnull=True)
        .select_related("site")
        .prefetch_related("languages", "attachments")
        .order_by("-period_start", "-issued_on", "-created_at", "-pk")
    )


def valid_on(document: HostDocument, on: datetime.date) -> bool:
    if document.period_start is not None and document.period_start > on:
        return False
    if document.period_end is not None and document.period_end < on:
        return False
    return document.valid_until is None or document.valid_until >= on


def issues(document: HostDocument, on: datetime.date, worker: ForeignWorker | None) -> list[str]:
    found: list[str] = []
    if not valid_on(document, on):
        found.append(Issue.OUT_OF_PERIOD)
    if document.kind in ISSUED_KINDS:
        if document.issued_on is None:
            found.append(Issue.ISSUED_ON_MISSING)
        elif document.issued_on < deadlines.add_months(on, -ISSUED_MONTHS):
            found.append(Issue.ISSUED_LONG_AGO)
    if document.kind == K.ANNUAL_CALENDAR:
        codes = {language.code for language in document.languages.all()}
        wanted = {JAPANESE}
        if worker is not None and worker.support_language is not None:
            wanted.add(worker.support_language.code)
        if not wanted <= codes:
            found.append(Issue.CALENDAR_LANGUAGE)
    return found


def select(
    documents: Iterable[HostDocument],
    row: Mapping[str, Any],
    site: HostSite | None,
    on: datetime.date,
    worker: ForeignWorker | None,
) -> tuple[HostDocument | None, list[str]]:
    """行に綴じる書類（問題の無いものを先に。無ければ最新のものと、その問題）。"""
    kinds = set(kinds_for_row(row))
    candidates = [
        document
        for document in documents
        if document.kind in kinds
        and document.attachments.filter(voided_at__isnull=True).exists()
        and (document.site_id is None or site is None or document.site_id == site.pk)
    ]
    if not candidates:
        return None, []
    checked = [(document, issues(document, on, worker)) for document in candidates]
    for document, found in checked:
        if not found:
            return document, []
    return checked[0]


def files_of(document: HostDocument) -> list[Attachment]:
    return list(document.attachments.filter(voided_at__isnull=True).order_by("created_at", "pk"))


def ending_soon(document: HostDocument, today: datetime.date) -> bool:
    end = document.period_end or document.valid_until
    return end is not None and today <= end <= today + datetime.timedelta(days=ENDING_DAYS)


# --- 登録・取消 ------------------------------------------------------------------------


def validate(document: HostDocument, files: list[File[Any]]) -> None:
    errors: dict[str, list[ValidationError]] = {}
    if document.site is not None and document.site.host_organization_id != (
        document.host_organization_id
    ):
        errors["site"] = [ValidationError(_("Select a site of this host organization."))]
    if document.kind in PERIOD_KINDS and not (document.period_start and document.period_end):
        errors["period_end"] = [
            ValidationError(_("Enter the period of the agreement or the calendar."), code="period")
        ]
    if (
        document.period_start
        and document.period_end
        and document.period_start > document.period_end
    ):
        errors["period_end"] = [
            ValidationError(_("The end of the period must be on or after the start."))
        ]
    if document.kind in ISSUED_KINDS and document.issued_on is None:
        errors["issued_on"] = [ValidationError(_("Enter the issue date."), code="issued_on")]
    if not files:
        errors["uploads"] = [ValidationError(_("Select a file."), code="required")]
    for file in files:
        try:
            attachments.validate_upload(AttachmentKind.HOST_DOCUMENT, file)
        except ValidationError as error:
            errors.setdefault("uploads", []).append(error)
    if errors:
        raise ValidationError(errors)


@transaction.atomic
def add(
    document: HostDocument,
    files: list[File[Any]],
    *,
    language_ids: Iterable[int],
    by: User | None,
) -> HostDocument:
    validate(document, files)
    document.created_by = by
    document.updated_by = by
    document.save()
    document.languages.set(list(language_ids))
    for file in files:
        attachments.create_attachment(
            target=document, kind=AttachmentKind.HOST_DOCUMENT, file=file, created_by=by
        )
    return document


@transaction.atomic
def void(document: HostDocument, *, by: User | None, reason: str) -> None:
    """取り消す（理由は必須。ファイルも取り消す。行とファイルは消さない）。"""
    if document.voided_at is not None:
        raise ValidationError(_("This document is already voided."), code="voided")
    if not reason.strip():
        raise ValidationError(_("Enter the reason for voiding."), code="void_reason")
    document.voided_at = timezone.now()
    document.voided_by = by
    document.void_reason = reason.strip()
    document.updated_by = by
    document.save(
        update_fields=["voided_at", "voided_by", "void_reason", "updated_by", "updated_at"]
    )
    for attachment in files_of(document):
        attachments.void_attachment(attachment, by=by, reason=reason)
