"""提出用の統合 PDF（T42、ADR-0033）。

申請の書類の一覧（申請の種類×基準日）の書類を、1 つの PDF にまとめる。
- 順（T42-D2）: 提出書類一覧表の行を先頭に、続いて表の順・番号順・行の順（一覧の定義の順）。
  一覧表の表紙の留意事項３「提出書類は、提出書類一覧表を先頭に番号順に並べた上で…」。
- 中身: 生成の行は、署名済みのスキャンが要る様式で署名済みならそのスキャン、ほかは生成した PDF
  （署名待ちは警告。T42-D5）。添付の行は添付のファイル。システムの外の行は、その場でアップロードした
  ファイル（T42-D3。別の添付としては保存しない）。同じ生成物は 1 回だけ綴じる（第1-6号と別紙）。
- 権限: 綴じる人が開けない書類は綴じない（要配慮の添付は担当者と workers.sensitive_documents を
  持つ人だけ。T42-D12）。要配慮を綴じたら contains_sensitive で、ダウンロードも同じ人に限る。
- 容量（T42-D6）: 在留申請オンラインシステムは顔写真を含め合計 25MB（設定 SUBMISSION_PDF_MAX_MB。
  MB は 1,000,000 バイト）。元のファイルの合計で見積もって警告し、作った後も超えたら警告を残す。
  作るのは止めない。画像は圧縮し直さない（T42-D7）。
- 作ったことは生成履歴（SubmissionBundle）と監査ログ（GENERATE）に、ダウンロードは DOWNLOAD に残す。
"""

from __future__ import annotations

import enum
import hashlib
import io
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from pypdf import PdfWriter

from apps.core.models import Action, Attachment, AuditAction, User
from apps.forms_engine.models import GeneratedDocument, SubmissionBundle
from apps.workers.models import ForeignWorker
from services import (
    application_sets,
    attachments,
    audit,
    authorization,
    form_generation,
    form_languages,
    form_signatures,
    host_documents,
    pdf_pages,
    submission_drafts,
    submission_lists,
)
from services.application_sets import Overview, RowState, Status
from services.authorization import AnyUser

LIST_MARK = "提出書類一覧表"  # 一覧表そのものの行（先頭に綴じる）
NOT_BOUND_MARK = "返信用封筒"  # 紙だけの行（アップロードの欄を出さない）
NOT_ALLOWED = "not_allowed"  # Part.error: 綴じる人が開けない（警告は別に出す）


class Source(enum.StrEnum):
    GENERATED = "generated"  # 生成した PDF
    SIGNED = "signed"  # 署名済みのスキャン
    ATTACHMENT = "attachment"  # 添付
    UPLOAD = "upload"  # その場でアップロードしたシステムの外の書類
    TRANSLATION = "translation"  # 自作の訳文の版の PDF（日本語の様式の直後。T43-D1）
    HOST_DOCUMENT = "host_document"  # 受入れ機関の書類の保管庫（T42 の追加）
    LIST = "list"  # システムで作った提出書類一覧表（T42 の追加）


class Warn(enum.StrEnum):
    NOT_GENERATED = "not_generated"
    GENERATING = "generating"
    OTHER_TARGET = "other_target"
    AWAITING_SIGNATURE = "awaiting_signature"
    MISSING = "missing"
    EXPIRED = "expired"
    OUT_OF_PERIOD = "out_of_period"
    DATE_MISSING = "date_missing"
    SENSITIVE_NOT_ALLOWED = "sensitive_not_allowed"
    NOT_ALLOWED = "not_allowed"
    UNREADABLE = "unreadable"
    OVER_LIMIT = "over_limit"
    TRANSLATION_MISSING = "translation_missing"


WARNING_TEXTS: Mapping[str, Any] = {
    Warn.NOT_GENERATED: gettext_lazy("Not generated, so it is not included."),
    Warn.GENERATING: gettext_lazy("Still being generated, so it is not included."),
    Warn.OTHER_TARGET: gettext_lazy(
        "Generated only for another contract or version, so it is not included."
    ),
    Warn.AWAITING_SIGNATURE: gettext_lazy(
        "Not signed yet: the generated PDF without the signature is included."
    ),
    Warn.MISSING: gettext_lazy("Not attached, so it is not included."),
    Warn.EXPIRED: gettext_lazy("The membership has expired, so it is not included."),
    Warn.OUT_OF_PERIOD: gettext_lazy("The examination was too long before the application."),
    Warn.DATE_MISSING: gettext_lazy("The examination date is not entered."),
    Warn.SENSITIVE_NOT_ALLOWED: gettext_lazy(
        "Only the assigned staff member and administrators can include this sensitive "
        "document, so it is not included."
    ),
    Warn.NOT_ALLOWED: gettext_lazy("You cannot open this file, so it is not included."),
    Warn.UNREADABLE: gettext_lazy(
        "The file cannot be read as a PDF or an image (broken or password-protected), so it "
        "is not included."
    ),
    Warn.TRANSLATION_MISSING: gettext_lazy(
        "The worker's support language is Chinese, but only the Japanese version of this form is "
        "generated (generate the bilingual version or the Chinese translation)."
    ),
    Warn.OVER_LIMIT: gettext_lazy(
        "The total size is over the limit of the online application system "
        "(%(limit)s MB including the photo). Attach what fits and submit the rest with the "
        "declaration on attaching documents (reference form 9)."
    ),
}
# 状態 → 綴じられないときの警告
STATUS_WARNINGS = {
    Status.NOT_GENERATED: Warn.NOT_GENERATED,
    Status.GENERATING: Warn.GENERATING,
    Status.OTHER_TARGET: Warn.OTHER_TARGET,
    Status.MISSING: Warn.MISSING,
    Status.EXPIRED: Warn.EXPIRED,
}
# 綴じるが確かめが要る状態（健康診断の書類の時期・受診日）
STATUS_NOTES = {Status.OUT_OF_PERIOD: Warn.OUT_OF_PERIOD, Status.DATE_MISSING: Warn.DATE_MISSING}


def warning_text(code: str) -> str:
    if code in host_documents.ISSUE_TEXTS:
        return host_documents.issue_text(code)
    text = str(WARNING_TEXTS[code])
    if code == Warn.OVER_LIMIT:
        return text % {"limit": settings.SUBMISSION_PDF_MAX_MB}
    return text


def limit_bytes() -> int:
    return int(settings.SUBMISSION_PDF_MAX_MB) * 1_000_000


@dataclass
class Part:
    """綴じるファイル 1 つ。"""

    source: Source
    object_id: int | None
    size: int
    sensitive: bool
    reader: Callable[[], bytes]
    _raw: bytes | None = None
    _pdf: bytes | None = None
    error: str = ""

    def load(self) -> None:
        """読み込んで PDF にする（読めなければ error）。何度呼んでも 1 回だけ読む。"""
        if self._pdf is not None or self.error:
            return
        try:
            self._raw = self.reader()
            self._pdf = pdf_pages.as_pdf(self._raw)
        except pdf_pages.UnreadableFile as unreadable:
            self.error = unreadable.code
        except OSError:
            self.error = "missing_file"

    @property
    def pdf(self) -> bytes:
        assert self._pdf is not None
        return self._pdf

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self._raw or b"").hexdigest()


@dataclass
class Entry:
    """一覧の行 1 つと、綴じるファイル。"""

    state: RowState
    parts: list[Part] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    same_as: str = ""  # 同じ生成物を先に綴じる行の ID
    can_upload: bool = False
    saved: list[Attachment] = field(default_factory=list)  # 下書きに保存したシステムの外の書類

    @property
    def row(self) -> dict[str, Any]:
        return self.state.row

    @property
    def row_id(self) -> str:
        return str(self.state.row["id"])

    @property
    def bound(self) -> list[Part]:
        return [part for part in self.parts if not part.error]

    @property
    def size(self) -> int:
        return sum(part.size for part in self.bound)

    @property
    def title(self) -> str:
        """しおりの名前（表・番号・書類の名前の 1 行目。個人情報を含まない）。"""
        document = str(self.row["document"]).splitlines()[0][:40]
        return f"{self.row['table']} {self.row['number']} {document}"


@dataclass
class Outline:
    overview: Overview
    entries: list[Entry]
    skip_table2: bool = False  # 既に受け入れている機関（受入れ機関の印。第2表は提出不要。T42-D25）
    # 印は無いが、台帳では同じ年度にほかの外国人の契約がある（受入れ機関の画面で確かめる案内）
    accepting_suggested: bool = False

    @property
    def list_row_id(self) -> str:
        """提出書類一覧表そのものの行。"""
        return next((e.row_id for e in self.entries if LIST_MARK in str(e.row["document"])), "")

    def default_marks(self, make_list: bool) -> dict[str, str]:
        """提出確認欄の有・無の初期値（T42-D19）。"""
        bound = {entry.row_id for entry in self.entries if entry.bound or entry.same_as}
        if make_list and self.list_row_id:
            bound.add(self.list_row_id)
        return submission_lists.default_marks(self.overview, bound, skip_table2=self.skip_table2)

    @property
    def bound_parts(self) -> list[Part]:
        return [part for entry in self.entries for part in entry.bound]

    @property
    def estimated_size(self) -> int:
        return sum(part.size for part in self.bound_parts)

    @property
    def over_limit(self) -> bool:
        return self.estimated_size > limit_bytes()

    @property
    def contains_sensitive(self) -> bool:
        return any(part.sensitive for part in self.bound_parts)

    @property
    def warnings(self) -> list[tuple[Entry, str]]:
        found = [(entry, code) for entry in self.entries for code in entry.warnings]
        found += [
            (entry, Warn.UNREADABLE)
            for entry in self.entries
            for part in entry.parts
            if part.error and part.error != NOT_ALLOWED
        ]
        return found

    @property
    def has_warnings(self) -> bool:
        return bool(self.warnings) or self.over_limit


def _file_reader(file: Any) -> Callable[[], bytes]:
    def read() -> bytes:
        with file.open("rb") as handle:
            data: bytes = handle.read()
        return data

    return read


def _document_parts(document: GeneratedDocument, user: AnyUser, entry: Entry) -> list[Part]:
    signed = form_signatures.state(document) == form_signatures.SignatureState.SIGNED
    if signed:
        latest = form_signatures.signed_on(document)
        scans = [
            row.attachment
            for row in form_signatures.live_signatures(document).order_by("pk")
            if row.signed_on == latest
        ]
        return [_attachment_part(scan, user, entry, Source.SIGNED) for scan in scans]
    if form_signatures.state(document) == form_signatures.SignatureState.AWAITING:
        entry.warnings.append(Warn.AWAITING_SIGNATURE)
    if not form_generation.can_download_pdf(user, document):
        entry.warnings.append(Warn.NOT_ALLOWED)
        return []
    return [
        Part(
            Source.GENERATED,
            document.pk,
            document.pdf_file.size if document.pdf_file else 0,
            False,
            _file_reader(document.pdf_file),
        )
    ]


def _translation_parts(
    document: GeneratedDocument, overview: Overview, user: AnyUser, entry: Entry
) -> list[Part]:
    """日本語の様式の直後に綴じる訳文（同じ対象の最新。無く、支援言語が中国語なら警告。T43）。"""
    translation = application_sets.translation_of(document, overview.facts)
    if translation is None:
        worker = overview.facts.worker
        if form_languages.needs_chinese_copy(
            worker, document.template.form_id, document.template.language
        ):
            entry.warnings.append(Warn.TRANSLATION_MISSING)
        return []
    if not form_generation.can_download_pdf(user, translation):
        entry.warnings.append(Warn.NOT_ALLOWED)
        return []
    return [
        Part(
            Source.TRANSLATION,
            translation.pk,
            translation.pdf_file.size if translation.pdf_file else 0,
            False,
            _file_reader(translation.pdf_file),
        )
    ]


def _attachment_part(attachment: Attachment, user: AnyUser, entry: Entry, source: Source) -> Part:
    part = Part(
        source,
        attachment.pk,
        attachment.size,
        attachment.is_sensitive,
        _file_reader(attachment.file),
    )
    if not attachments.can_download(user, attachment):
        code = Warn.SENSITIVE_NOT_ALLOWED if attachment.is_sensitive else Warn.NOT_ALLOWED
        entry.warnings.append(code)
        part.error = NOT_ALLOWED
    return part


def _ordered(states: Sequence[RowState]) -> list[RowState]:
    head = [state for state in states if LIST_MARK in str(state.row["document"])]
    return head + [state for state in states if state not in head]


def outline(overview: Overview, user: AnyUser) -> Outline:
    """綴じる順と中身（ファイルは読み込んで、読めるかを確かめる）。

    システムの外の行は、下書きに保存したファイル（submission_drafts）を綴じる。
    """
    saved = submission_drafts.files_by_row(
        submission_drafts.draft_for(overview.facts.worker, overview.definition.id)
    )
    entries: list[Entry] = []
    bound_documents: dict[int, str] = {}
    facts = overview.facts
    host = facts.contract.host_organization if facts.contract is not None else None
    accepting = host is not None and submission_lists.already_accepting(host, facts.on)
    suggested = (
        host is not None
        and not accepting
        and submission_lists.suggested_since(host, facts.on, worker_id=facts.worker.pk) is not None
    )
    for state in _ordered(overview.rows):
        entry = Entry(state=state)
        entries.append(entry)
        how = state.row["how"]
        if state.status in STATUS_WARNINGS and state.applicable is True:
            entry.warnings.append(STATUS_WARNINGS[state.status])
        if state.status in STATUS_NOTES:
            entry.warnings.append(STATUS_NOTES[state.status])
        if state.host_document is not None:
            entry.warnings += list(state.issues)
            entry.parts = [
                _attachment_part(attachment, user, entry, Source.HOST_DOCUMENT)
                for attachment in host_documents.files_of(state.host_document)
            ]
        if "out" in how:
            entry.can_upload = NOT_BOUND_MARK not in str(state.row["document"])
            for attachment in saved.get(entry.row_id, ()):
                entry.parts.append(_attachment_part(attachment, user, entry, Source.UPLOAD))
            entry.saved = list(saved.get(entry.row_id, ()))
        elif "generate" in how:
            document = state.document
            if state.status == Status.GENERATED and document is not None:
                if document.pk in bound_documents:
                    entry.same_as = bound_documents[document.pk]
                else:
                    bound_documents[document.pk] = entry.row_id
                    entry.parts = _document_parts(document, user, entry)
                    entry.parts += _translation_parts(document, overview, user, entry)
        elif state.host_document is None and state.status in (Status.ATTACHED, *STATUS_NOTES):
            entry.parts = [
                _attachment_part(attachment, user, entry, Source.ATTACHMENT)
                for attachment in application_sets.row_attachments(state.row, overview.facts)
            ]
        if accepting and entry.parts and submission_lists.is_table2(state.row):
            entry.warnings.append(host_documents.Issue.TABLE2_NOT_NEEDED)
    found = Outline(
        overview=overview, entries=entries, skip_table2=accepting, accepting_suggested=suggested
    )
    for part in found.bound_parts:
        part.load()
    return found


def build(
    outline_: Outline,
    *,
    by: User | None,
    accept_warnings: bool,
    make_list: bool = False,
    marks: Mapping[str, str] | None = None,
) -> SubmissionBundle:
    """1 つの PDF にまとめ、生成履歴と監査ログに残す。

    make_list のときは、提出書類一覧表（表紙・第1〜3表）を marks の印で作って先頭に綴じる。
    """
    if outline_.has_warnings and not accept_warnings:
        raise ValidationError(_("Check the warnings before making the PDF."), code="warnings")
    if not outline_.bound_parts and not make_list:
        raise ValidationError(_("There is no document to include."), code="empty")
    overview = outline_.overview
    marks = dict(marks if marks is not None else outline_.default_marks(make_list))
    writer = PdfWriter()
    items: list[dict[str, Any]] = []
    if make_list:
        facts = overview.facts
        contract = facts.contract
        books = submission_lists.workbooks(
            overview,
            marks,
            worker_name=facts.worker.name_romaji,
            host_name=contract.host_organization.name if contract else "",
        )
        with tempfile.TemporaryDirectory(prefix="list-") as workdir:
            for index, pdf in enumerate(submission_lists.render(books, Path(workdir))):
                start = len(writer.pages)
                writer.append(
                    str(pdf),
                    outline_item=_("List of documents to submit") if index == 0 else None,
                    import_outline=False,
                )
                items.append(
                    {
                        "row": outline_.list_row_id,
                        "table": "",
                        "number": "",
                        "source": str(Source.LIST),
                        "object_id": None,
                        "pages": len(writer.pages) - start,
                        "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
                        "file": books[index].file,
                    }
                )
    for entry in outline_.entries:
        for index, part in enumerate(entry.bound):
            start = len(writer.pages)
            writer.append(
                io.BytesIO(part.pdf),
                outline_item=entry.title if index == 0 else None,
                import_outline=False,
            )
            items.append(
                {
                    "row": entry.row_id,
                    "table": entry.row["table"],
                    "number": entry.row["number"],
                    "source": str(part.source),
                    "object_id": part.object_id,
                    "pages": len(writer.pages) - start,
                    "sha256": part.sha256,
                }
            )
    writer.add_metadata({"/Title": f"提出書類一式（{overview.definition.name}）"})
    out = io.BytesIO()
    writer.write(out)
    data = out.getvalue()
    warnings = [{"row": entry.row_id, "code": str(code)} for entry, code in outline_.warnings]
    if len(data) > limit_bytes():
        warnings.append({"row": "", "code": str(Warn.OVER_LIMIT)})
    facts = overview.facts
    bundle = SubmissionBundle(
        worker=facts.worker,
        set_id=overview.definition.id,
        definition_valid_from=overview.definition.valid_from,
        basis_on=facts.on,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        page_count=len(writer.pages),
        contains_sensitive=outline_.contains_sensitive,
        items=items,
        warnings=warnings,
        list_marks={key: value for key, value in marks.items() if value} if make_list else {},
        created_by=by,
        updated_by=by,
    )
    bundle.file.save(f"{overview.definition.id}.pdf", ContentFile(data), save=False)
    with audit.event(
        AuditAction.GENERATE,
        target=bundle,
        actor=by,
        context={
            "submission_bundle": True,
            "set_id": bundle.set_id,
            "pages": bundle.page_count,
            "items": len(items),
            "sensitive": bundle.contains_sensitive,
        },
    ):
        bundle.save()
    return bundle


def bundles_of(worker: ForeignWorker) -> list[SubmissionBundle]:
    return list(worker.submission_bundles.select_related("created_by").order_by("-created_at")[:10])


def can_view(user: AnyUser, bundle: SubmissionBundle) -> bool:
    """統合 PDF を見られるか（forms.generate と、対象の外国人を見る権限）。"""
    return can_view_worker(user, bundle.worker_id)


def can_view_worker(user: AnyUser, worker_id: int) -> bool:
    if not authorization.has_action(user, Action.FORMS_GENERATE):
        return False
    if not authorization.has_action(user, Action.WORKERS_VIEW):
        return False
    return authorization.scope(user, ForeignWorker.objects.filter(pk=worker_id)).exists()


def is_assigned_staff(user: AnyUser, worker: ForeignWorker) -> bool:
    staff = worker.assigned_staff
    return (
        isinstance(user, User)
        and staff is not None
        and staff.user_id is not None
        and staff.user_id == user.pk
    )


def can_download(user: AnyUser, bundle: SubmissionBundle) -> bool:
    """ダウンロードできるか。要配慮を含むときは、さらに担当者か workers.sensitive_documents。"""
    if not can_view(user, bundle):
        return False
    if not bundle.contains_sensitive:
        return True
    return is_assigned_staff(user, bundle.worker) or authorization.has_action(
        user, Action.WORKERS_SENSITIVE_DOCUMENTS
    )
