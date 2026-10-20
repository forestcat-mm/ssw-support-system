"""生成物の画面と、PDF の認可つきのダウンロード（T29・T31、ADR-0018）。

PDF のダウンロードはファイルの中身を渡す唯一の経路。記入済みの docx・xlsx は画面から取り出さない
（段階4 の F13 で決める）。
"""

from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import FileResponse, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views import View

from apps.contracts.models import EmploymentContract, EmploymentContractVersion
from apps.core.models import Action, Attachment, AuditAction, User
from apps.core.permissions import ActionRequiredMixin
from apps.core.web_forms import consume_once_token, once_context
from apps.forms_engine.forms import (
    NOT_LOADED,
    ApplicationSetForm,
    BulkGenerateForm,
    ConsultationRecordForm,
    PublicDutyPledgeForm,
    SignatureForm,
    SubmissionBundleForm,
)
from apps.forms_engine.models import GeneratedDocument, GenerationStatus, SubmissionBundle
from apps.hosts.models import HostOrganization
from apps.support.models import Briefing, DeliveryKind, PeriodicInterview, SupportPlanVersion
from apps.support.record_forms import ReasonForm
from apps.workers.views import WorkerDetailView, WorkerFormView
from services import (
    application_bulk,
    application_sets,
    attachments,
    audit,
    authorization,
    deliveries,
    form_data_application,
    form_data_consultation,
    form_generation,
    form_signatures,
    form_templates,
    submission_bundles,
    submission_drafts,
    workers,
)
from services.files import FilePolicy, validate_file


class GeneratedDocumentView(ActionRequiredMixin, View):
    """生成物 1 件（状態・様式の版・基準日・生成のときの警告・PDF・交付記録を作る）。"""

    required_action = Action.FORMS_GENERATE

    def get(self, request: HttpRequest, pk: int) -> TemplateResponse:
        document = get_object_or_404(
            GeneratedDocument.objects.select_related("template", "worker", "created_by"), pk=pk
        )
        if not form_generation.can_view(request.user, document):
            raise PermissionDenied
        target = document.target
        version = target if isinstance(target, SupportPlanVersion) else None
        # 交付記録を作れる生成物: 支援計画書（写し）と、契約を対象にした自機関の書類（T36）
        delivery_kind: str | None = DeliveryKind.SUPPORT_PLAN_COPY if version is not None else None
        if isinstance(target, EmploymentContract):
            delivery_kind = next(
                (
                    kind
                    for kind, form_id in deliveries.CONTRACT_FORM_IDS.items()
                    if form_id == document.template.form_id
                ),
                None,
            )
        can_deliver = (
            delivery_kind is not None
            and document.status == GenerationStatus.COMPLETED
            and document.worker is not None
            and workers.is_active(document.worker)
            and authorization.has_action(request.user, Action.SUPPORT_MANAGE)
        )
        if document.worker is not None:
            audit.record_view(document.worker, context={"generated_document": document.pk})
        elif isinstance(target, HostOrganization):  # 相談記録書（複数の外国人。T35）
            audit.record_view(target, context={"generated_document": document.pk})
        back_url = ""
        if version is not None and document.worker is not None:
            back_url = (
                reverse("workers:support", args=[document.worker.pk])
                + f"?plan={version.plan_id}&version={version.version}"
            )
        elif isinstance(target, Briefing):
            back_url = reverse("workers:support_briefing", args=[target.worker_id, target.pk])
        elif isinstance(target, PeriodicInterview):
            back_url = reverse("workers:support_interview", args=[target.worker_id, target.pk])
        elif isinstance(target, HostOrganization):
            back_url = reverse("hosts:detail", args=[target.pk]) + "#consultation-records"
        elif isinstance(target, EmploymentContract) and document.worker is not None:
            back_url = reverse("workers:contracts", args=[document.worker.pk]) + (
                f"?contract={target.pk}"
            )
        elif isinstance(target, EmploymentContractVersion) and document.worker is not None:
            back_url = (
                reverse("workers:contracts", args=[document.worker.pk])
                + f"?contract={target.contract_id}&version={target.version}"
            )
        elif document.worker is not None:
            back_url = reverse("workers:detail", args=[document.worker.pk])
        context: dict[str, Any] = {
            "document": document,
            "worker": document.worker,
            "plan_version": version,
            "contract_version": target if isinstance(target, EmploymentContractVersion) else None,
            "back_url": back_url,
            "can_download": form_generation.can_download_pdf(request.user, document),
            "can_deliver": can_deliver,
            "delivery_kind": delivery_kind,
            "signature_state": form_signatures.state(document),
            "signed_on": form_signatures.signed_on(document),
            "can_sign": form_signatures.can_add_signature(document),
            "stale": form_generation.is_stale(document),
            "stale_minutes": settings.FORM_STALE_AFTER // 60,
        }
        return TemplateResponse(request, "forms_engine/document.html", context)


class GeneratedPdfDownloadView(View):
    def get(self, request: HttpRequest, pk: int) -> FileResponse:
        document = get_object_or_404(GeneratedDocument.objects.select_related("template"), pk=pk)
        if not form_generation.can_download_pdf(request.user, document):
            raise PermissionDenied
        filename = f"{document.template.form_id}_{document.basis_on:%Y%m%d}_{document.pk}.pdf"
        response = FileResponse(document.pdf_file.open("rb"), as_attachment=True, filename=filename)
        audit.record(
            AuditAction.DOWNLOAD,
            target=document,
            context={"form_id": document.template.form_id, "file": "pdf"},
        )
        return response


# --- 帳票タブと署名（T33、ADR-0022）-------------------------------------------------------


class WorkerDocumentsView(WorkerDetailView):
    """外国人詳細の「帳票」タブ（生成物ごとの状態・署名日・PDF・署名済みのスキャン）。"""

    tab = "documents"
    template_name = "forms_engine/worker_documents.html"

    def tab_context(self, context: dict[str, Any]) -> dict[str, Any]:
        rows = form_signatures.rows_for(self.worker)
        legacy = form_signatures.legacy_rows(self.worker)
        user = self.request.user
        context.update(
            document_rows=[
                {
                    "row": row,
                    "can_download": form_generation.can_download_pdf(user, row.document),
                    "can_sign": form_signatures.can_add_signature(row.document),
                }
                for row in rows
            ],
            legacy_rows=legacy,
            stale_minutes=settings.FORM_STALE_AFTER // 60,
        )
        return {"shown": {"generated_documents": [row.document.pk for row in rows]}}


class ApplicationSetView(WorkerDetailView):
    """「帳票」タブの申請の書類の一覧（認定・変更・更新。T37、ADR-0026）。

    入管の提出書類一覧・確認表の全部の行を、当てはまる行と当てはまらない行に分けて出す。生成する様式は
    生成の有無と署名の状態、添付は有無（健康診断の書類は有無と日付だけ）、ほかはシステムの外。
    """

    tab = "documents"
    template_name = "forms_engine/application_set.html"

    def tab_context(self, context: dict[str, Any]) -> dict[str, Any]:
        today = timezone.localdate()
        params = self.request.GET
        form = ApplicationSetForm(
            {
                "set_id": params.get("set_id") or application_sets.default_set(self.worker),
                "basis_on": params.get("basis_on") or today.isoformat(),
            }
        )
        if form.is_valid():
            set_id, on = form.cleaned_data["set_id"], form.cleaned_data["basis_on"]
        else:
            set_id, on = application_sets.default_set(self.worker), today
        found = application_sets.overview(self.worker, set_id, on)
        user = self.request.user
        bundles = submission_bundles.bundles_of(self.worker)
        context.update(
            selector=form,
            overview=found,
            set_id=set_id,
            basis_on=on,
            row_checks=application_bulk.row_checks(found),
            bundles=[
                {"bundle": bundle, "can_download": submission_bundles.can_download(user, bundle)}
                for bundle in bundles
            ],
            can_generate=workers.is_active(self.worker)
            and authorization.has_action(user, Action.FORMS_GENERATE),
            query=urlencode({"set_id": set_id, "basis_on": on.isoformat()}),
        )
        return {
            "shown": {
                "application_set": set_id,
                "basis_on": on.isoformat(),
                "generated_documents": found.shown_documents,
            }
        }


def apply_errors(form: Any, error: ValidationError) -> None:
    """サービスのエラーを入力欄に戻す（確認書の添付の「confirmation」はファイルの欄）。"""
    if not hasattr(error, "error_dict"):
        form.add_error(None, error)
        return
    for key, messages_ in error.error_dict.items():
        name = "scans" if key in ("confirmation", "files") else key
        form.add_error(name if name in form.fields else None, messages_)


class SignatureAddView(WorkerFormView):
    """署名済みのスキャンを添付する（署名日は必須。T33-D1・D2）。"""

    required_action = Action.FORMS_GENERATE
    template_name = "forms_engine/signature_form.html"

    def document(self, worker: Any, document_id: int) -> GeneratedDocument:
        document = get_object_or_404(
            GeneratedDocument.objects.select_related("template"), pk=document_id, worker=worker
        )
        if not form_signatures.can_add_signature(document):
            raise PermissionDenied
        return document

    def get(self, request: HttpRequest, pk: int, document_id: int) -> HttpResponse:
        worker = self.worker(request, pk)
        document = self.document(worker, document_id)
        form = SignatureForm(initial={"signed_on": timezone.localdate()})
        return self.render(request, worker=worker, document=document, form=form)

    def post(self, request: HttpRequest, pk: int, document_id: int) -> HttpResponse:
        worker = self.worker(request, pk)
        document = self.document(worker, document_id)
        back = reverse("workers:documents", args=[worker.pk])
        if not consume_once_token(request):
            return self.already_submitted(request, back)
        form = SignatureForm(request.POST, request.FILES)
        if form.is_valid():
            assert isinstance(request.user, User)
            try:
                form_signatures.add_signature(
                    document,
                    form.cleaned_data["scans"],
                    signed_on=form.cleaned_data["signed_on"],
                    by=request.user,
                )
            except ValidationError as error:
                apply_errors(form, error)
            else:
                messages.success(request, _("Attached the signed copy."))
                return redirect(back)
        return self.render(request, worker=worker, document=document, form=form)


class SignatureVoidView(WorkerFormView):
    """生成物に添付した署名済みのスキャンを取り消す（理由は必須。確認書の添付は記録の画面で取り消す）。"""

    required_action = Action.FORMS_GENERATE
    template_name = "forms_engine/signature_void.html"

    def target(
        self, request: HttpRequest, pk: int, document_id: int, attachment_id: int
    ) -> tuple[Any, GeneratedDocument, Attachment]:
        worker = self.worker(request, pk)
        document = get_object_or_404(GeneratedDocument, pk=document_id, worker=worker)
        attachment = get_object_or_404(
            document.attachments.filter(voided_at__isnull=True), pk=attachment_id
        )
        return worker, document, attachment

    def get(
        self, request: HttpRequest, pk: int, document_id: int, attachment_id: int
    ) -> HttpResponse:
        worker, document, attachment = self.target(request, pk, document_id, attachment_id)
        return self.render(
            request, worker=worker, document=document, attachment=attachment, form=ReasonForm()
        )

    def post(
        self, request: HttpRequest, pk: int, document_id: int, attachment_id: int
    ) -> HttpResponse:
        worker, document, attachment = self.target(request, pk, document_id, attachment_id)
        back = reverse("workers:documents", args=[worker.pk])
        if not consume_once_token(request):
            return self.already_submitted(request, back)
        form = ReasonForm(request.POST)
        if form.is_valid():
            assert isinstance(request.user, User)
            attachments.void_attachment(
                attachment, by=request.user, reason=form.cleaned_data["reason"]
            )
            messages.success(request, _("Voided the signed copy."))
            return redirect(back)
        return self.render(
            request, worker=worker, document=document, attachment=attachment, form=form
        )


# --- 相談記録書（第5-4号。T35、ADR-0024）------------------------------------------------------


def consultation_documents(host: HostOrganization) -> list[GeneratedDocument]:
    """受入れ機関の相談記録書の生成物（新しい順）。"""
    content_type = ContentType.objects.get_for_model(HostOrganization)
    return list(
        GeneratedDocument.objects.filter(
            template__form_id=CONSULTATION_FORM_ID, content_type=content_type, object_id=host.pk
        )
        .select_related("template", "created_by")
        .order_by("-created_at", "-pk")[:10]
    )


CONSULTATION_FORM_ID = "5-4"


class ConsultationRecordView(ActionRequiredMixin, View):
    """受入れ機関×期間の相談記録書を生成する（9 件ごとに 1 つの生成物。T35-D9・D10）。

    複数の外国人の個人情報を含むので、帳票の生成と外国人の閲覧の両方の権限が要る。
    """

    required_action = Action.FORMS_GENERATE
    template_name = "forms_engine/consultation_record.html"

    def host(self, request: HttpRequest, pk: int) -> HostOrganization:
        if not authorization.has_action(request.user, Action.WORKERS_VIEW):
            raise PermissionDenied
        return get_object_or_404(
            authorization.scope(request.user, HostOrganization.objects.all()), pk=pk
        )

    def page(
        self, request: HttpRequest, host: HostOrganization, form: ConsultationRecordForm
    ) -> HttpResponse:
        rows: list[Any] = []
        if form.is_bound and form.is_valid():
            rows = form_data_consultation.consultations_for(
                host, form.cleaned_data["starts_on"], form.cleaned_data["ends_on"]
            )
            audit.record_view(host, context={"consultations": [row.pk for row in rows]})
        return TemplateResponse(
            request,
            self.template_name,
            {
                "host": host,
                "form": form,
                "rows": rows,
                "documents": len(form_data_consultation.pages(rows)),
                "back_url": reverse("hosts:detail", args=[host.pk]),
                **once_context(request),
            },
        )

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        host = self.host(request, pk)
        starts_on, ends_on = form_data_consultation.default_period(timezone.localdate())
        form = ConsultationRecordForm(
            {"starts_on": starts_on.isoformat(), "ends_on": ends_on.isoformat()}
        )
        return self.page(request, host, form)

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        host = self.host(request, pk)
        form = ConsultationRecordForm(request.POST)
        if not form.is_valid() or request.POST.get("action") != "generate":
            return self.page(request, host, form)
        back = reverse("hosts:detail", args=[host.pk])
        if not consume_once_token(request):
            messages.info(request, _("This form has already been submitted."))
            return redirect(back)
        starts_on, ends_on = form.cleaned_data["starts_on"], form.cleaned_data["ends_on"]
        rows = form_data_consultation.consultations_for(host, starts_on, ends_on)
        if not rows:
            form.add_error(None, _("There are no consultations in this period."))
            return self.page(request, host, form)
        assert isinstance(request.user, User)
        try:
            with transaction.atomic():
                for page in form_data_consultation.pages(rows):
                    form_generation.request(
                        CONSULTATION_FORM_ID,
                        target=host,
                        data=form_data_consultation.build(starts_on, ends_on, page),
                        basis_on=ends_on,
                        by=request.user,
                    )
        except form_templates.FormTemplateError:
            form.add_error(None, NOT_LOADED)
            return self.page(request, host, form)
        messages.success(request, _("Requested the generation of the consultation records."))
        return redirect(f"{back}#consultation-records")


# --- 不足の書類の一括生成と提出用の統合 PDF（T42、ADR-0033）-----------------------------------


def _set_and_day(worker: Any, data: Any) -> tuple[str, Any]:
    """申請の種類と基準日（一覧の条件。不正なら初期値）。"""
    form = ApplicationSetForm(
        {
            "set_id": data.get("set_id") or application_sets.default_set(worker),
            "basis_on": data.get("basis_on") or timezone.localdate().isoformat(),
        }
    )
    if form.is_valid():
        return form.cleaned_data["set_id"], form.cleaned_data["basis_on"]
    return application_sets.default_set(worker), timezone.localdate()


def _set_url(worker: Any, set_id: str, on: Any) -> str:
    query = urlencode({"set_id": set_id, "basis_on": on.isoformat()})
    return reverse("workers:application_set", args=[worker.pk]) + f"?{query}"


class ApplicationBulkView(WorkerFormView):
    """不足の書類をまとめて生成する（T42）。

    行ごとの検査の結果（生成不可・警告）と不足の添付を一覧で出し、警告を確かめてから生成する。
    生成不可の行は選べず、飛ばして知らせる。
    """

    required_action = Action.FORMS_GENERATE
    template_name = "forms_engine/application_bulk.html"

    def page(
        self,
        request: HttpRequest,
        worker: Any,
        overview: application_sets.Overview,
        found: list[application_bulk.Candidate],
        form: BulkGenerateForm,
    ) -> HttpResponse:
        audit.record_view(
            worker,
            context={
                "application_bulk": overview.definition.id,
                "basis_on": str(overview.facts.on),
            },
        )
        return self.render(
            request,
            worker=worker,
            overview=overview,
            candidates=found,
            gaps=application_bulk.attachment_gaps(overview),
            form=form,
            has_warnings=any(c.warnings and not c.blocked and not c.same_as for c in found),
            has_plan=any(c.form_id == "1-17" and not c.same_as for c in found),
            selectable=[c for c in found if not c.blocked and not c.same_as],
            back_url=_set_url(worker, overview.definition.id, overview.facts.on),
        )

    def form(
        self, found: list[application_bulk.Candidate], *args: Any, **kwargs: Any
    ) -> BulkGenerateForm:
        choices = [(c.row_id, c.row_id) for c in found if not c.blocked and not c.same_as]
        return BulkGenerateForm(*args, row_choices=choices, **kwargs)

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        worker = self.worker(request, pk)
        set_id, on = _set_and_day(worker, request.GET)
        overview = application_sets.overview(worker, set_id, on)
        found = application_bulk.candidates(overview)
        initial = {
            "set_id": set_id,
            "basis_on": on,
            "rows": [c.row_id for c in found if not c.blocked and not c.same_as],
        }
        return self.page(request, worker, overview, found, self.form(found, initial=initial))

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        worker = self.worker(request, pk)
        set_id, on = _set_and_day(worker, request.POST)
        overview = application_sets.overview(worker, set_id, on)
        propriety = request.POST.get("propriety") in ("on", "true", "1")
        found = application_bulk.candidates(overview, propriety=propriety)
        form = self.form(found, request.POST)
        if not form.is_valid() or request.POST.get("action") != "generate":
            return self.page(request, worker, overview, found, form)
        back = _set_url(worker, set_id, on)
        if not consume_once_token(request):
            return self.already_submitted(request, back)
        if not form.cleaned_data["rows"]:
            form.add_error("rows", _("Select the documents to generate."))
            return self.page(request, worker, overview, found, form)
        assert isinstance(request.user, User)
        try:
            outcome = application_bulk.generate(
                found,
                form.cleaned_data["rows"],
                basis_on=on,
                by=request.user,
                accept_warnings=bool(form.cleaned_data.get("accept_warnings")),
            )
        except form_generation.WarningsNotAccepted:
            form.add_error("accept_warnings", _("Check the warnings before generating."))
            return self.page(request, worker, overview, found, form)
        if outcome.requested:
            messages.success(
                request,
                _("Requested the generation of %(count)s documents.")
                % {"count": len(outcome.requested)},
            )
        for skipped in outcome.skipped:
            messages.warning(
                request,
                _("Skipped %(form)s because it cannot be generated.") % {"form": skipped.form_id},
            )
        return redirect(back)


def upload_policy() -> FilePolicy:
    """その場でアップロードする書類（PDF・PNG・JPEG。1 つの大きさは容量の上限まで）。"""
    return FilePolicy(
        extensions=frozenset({"pdf", "png", "jpg", "jpeg"}),
        max_bytes=submission_bundles.limit_bytes(),
    )


class SubmissionBundleCreateView(WorkerFormView):
    """提出用の統合 PDF を作る（T42）。

    綴じる順の一覧・見積もりの大きさ・警告を出し、システムの外の行はその場でアップロードできる。
    """

    required_action = Action.FORMS_GENERATE
    template_name = "forms_engine/submission_bundle_form.html"

    def page(
        self,
        request: HttpRequest,
        worker: Any,
        outline: submission_bundles.Outline,
        form: SubmissionBundleForm,
    ) -> HttpResponse:
        overview = outline.overview
        audit.record_view(
            worker,
            context={
                "submission_outline": overview.definition.id,
                "basis_on": str(overview.facts.on),
            },
        )
        return self.render(
            request,
            worker=worker,
            outline=outline,
            overview=overview,
            form=form,
            rows=[
                {
                    "entry": entry,
                    "upload": form.upload_field(entry.row_id),
                    "saved": [
                        {"attachment": attachment, "remove": form.remove_field(attachment.pk)}
                        for attachment in entry.saved
                    ],
                    "mark": form.mark_field(entry.row_id),
                    "warnings": [submission_bundles.warning_text(code) for code in entry.warnings]
                    + [
                        submission_bundles.warning_text(submission_bundles.Warn.UNREADABLE)
                        for part in entry.parts
                        if part.error and part.error != submission_bundles.NOT_ALLOWED
                    ],
                }
                for entry in outline.entries
            ],
            limit_mb=settings.SUBMISSION_PDF_MAX_MB,
            max_files=settings.SUBMISSION_MAX_FILES,
            over_limit_text=submission_bundles.warning_text(submission_bundles.Warn.OVER_LIMIT),
            back_url=_set_url(worker, overview.definition.id, overview.facts.on),
        )

    def form(
        self, outline: submission_bundles.Outline, *args: Any, **kwargs: Any
    ) -> SubmissionBundleForm:
        rows = [entry.row_id for entry in outline.entries if entry.can_upload]
        marks = [entry.row_id for entry in outline.entries if entry.row.get("check")]
        saved = [attachment.pk for entry in outline.entries for attachment in entry.saved]
        return SubmissionBundleForm(
            *args, upload_rows=rows, mark_rows=marks, saved_files=saved, **kwargs
        )

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        worker = self.worker(request, pk)
        set_id, on = _set_and_day(worker, request.GET)
        overview = application_sets.overview(worker, set_id, on)
        outline = submission_bundles.outline(overview, request.user)
        initial: dict[str, Any] = {"set_id": set_id, "basis_on": on, "make_list": True}
        initial |= {f"mark-{row}": mark for row, mark in outline.default_marks(True).items()}
        form = self.form(outline, initial=initial)
        return self.page(request, worker, outline, form)

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """アップロードを下書きに保存し（取り除く印のファイルは取消）、「PDF を作る」なら作る。"""
        worker = self.worker(request, pk)
        set_id, on = _set_and_day(worker, request.POST)
        overview = application_sets.overview(worker, set_id, on)
        outline = submission_bundles.outline(overview, request.user)
        form = self.form(outline, request.POST, request.FILES)
        if not form.is_valid():
            return self.page(request, worker, outline, form)
        policy = upload_policy()
        uploads = form.uploads()
        for row_id, files in uploads.items():
            for file in files:
                try:
                    validate_file(file, policy)
                except ValidationError as error:
                    form.add_error(f"upload-{row_id}", error)
        for row_id in submission_drafts.readable_errors(uploads):
            form.add_error(
                f"upload-{row_id}",
                _("The file cannot be read as a PDF or an image (broken or password-protected)."),
            )
        if form.errors:
            return self.page(request, worker, outline, form)
        back = _set_url(worker, set_id, on)
        if not consume_once_token(request):
            return self.already_submitted(request, back)
        assert isinstance(request.user, User)
        try:
            submission_drafts.save(worker, set_id, uploads, remove=form.removed(), by=request.user)
        except ValidationError as error:
            apply_errors(form, error)
            return self.page(request, worker, outline, form)
        overview = application_sets.overview(worker, set_id, on)
        outline = submission_bundles.outline(overview, request.user)
        if request.POST.get("action") != "make":
            messages.success(request, _("Saved the files for the submission."))
            return redirect(
                reverse("workers:submission_bundle_create", args=[worker.pk])
                + f"?{urlencode({'set_id': set_id, 'basis_on': on.isoformat()})}"
            )
        try:
            bundle = submission_bundles.build(
                outline,
                by=request.user,
                accept_warnings=bool(form.cleaned_data.get("accept_warnings")),
                make_list=bool(form.cleaned_data.get("make_list")),
                marks=form.marks(),
            )
        except ValidationError as error:
            field = "accept_warnings" if error.code == "warnings" else None
            form = self.form(outline, request.POST)  # 保存したので、ファイルの欄は空にして出し直す
            form.is_valid()
            form.add_error(field, error)
            return self.page(request, worker, outline, form)
        messages.success(request, _("Made the PDF for the submission."))
        return redirect("workers:submission_bundle", pk=worker.pk, bundle_id=bundle.pk)


class SubmissionBundleView(WorkerDetailView):
    """作った統合 PDF 1 件（綴じた中身・警告・ダウンロード）。"""

    tab = "documents"
    template_name = "forms_engine/submission_bundle.html"

    def tab_context(self, context: dict[str, Any]) -> dict[str, Any]:
        user = self.request.user
        bundle = get_object_or_404(
            SubmissionBundle.objects.select_related("created_by"),
            pk=self.kwargs["bundle_id"],
            worker=self.worker,
        )
        if not submission_bundles.can_view(user, bundle):
            raise PermissionDenied
        definition = application_sets.definition(bundle.set_id, bundle.basis_on)
        names = {row["id"]: row for row in definition.rows}
        context.update(
            bundle=bundle,
            items=[{"item": item, "row": names.get(item["row"])} for item in bundle.items],
            warnings=[
                {"row": names.get(w["row"]), "text": submission_bundles.warning_text(w["code"])}
                for w in bundle.warnings
            ],
            can_download=submission_bundles.can_download(user, bundle),
            back_url=_set_url(self.worker, bundle.set_id, bundle.basis_on),
        )
        return {"shown": {"submission_bundle": bundle.pk}}


class SubmissionBundleDownloadView(View):
    def get(self, request: HttpRequest, pk: int, bundle_id: int) -> FileResponse:
        bundle = get_object_or_404(
            SubmissionBundle.objects.select_related("worker__assigned_staff"),
            pk=bundle_id,
            worker_id=pk,
        )
        if not submission_bundles.can_download(request.user, bundle):
            raise PermissionDenied
        filename = f"submission_{bundle.set_id}_{bundle.basis_on:%Y%m%d}_{bundle.pk}.pdf"
        response = FileResponse(bundle.file.open("rb"), as_attachment=True, filename=filename)
        audit.record(
            AuditAction.DOWNLOAD,
            target=bundle,
            context={"submission_bundle": True, "sensitive": bundle.contains_sensitive},
        )
        return response


# --- 第1-26号 公的義務履行に関する誓約書（T42 の追加）----------------------------------------


class PublicDutyPledgeView(WorkerFormView):
    """申請人の公的義務履行に関する誓約書を生成する（理由を入れる。署名は申請人が手で書く）。"""

    required_action = Action.FORMS_GENERATE
    template_name = "forms_engine/public_duty_pledge.html"
    form_id = "1-26"

    def data(self, form: PublicDutyPledgeForm) -> dict[str, Any]:
        relief_on, other = form.data_values()
        return form_data_application.build_1_26(
            form.cleaned_data["basis_on"], relief_on=relief_on, other_reason=other
        )

    def check(self, worker: Any, form: PublicDutyPledgeForm) -> Any:
        try:
            return form_generation.check(
                self.form_id,
                target=worker,
                data=self.data(form),
                basis_on=form.cleaned_data["basis_on"],
            )
        except form_templates.FormTemplateError:
            form.add_error(None, NOT_LOADED)
            return None

    def page(self, request: HttpRequest, worker: Any, form: Any, result: Any) -> HttpResponse:
        return self.render(
            request,
            worker=worker,
            form=form,
            result=result,
            back_url=reverse("workers:application_set", args=[worker.pk]),
        )

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        worker = self.worker(request, pk)
        form = PublicDutyPledgeForm(initial={"basis_on": timezone.localdate()})
        return self.page(request, worker, form, None)

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        worker = self.worker(request, pk)
        form = PublicDutyPledgeForm(request.POST)
        if not form.is_valid():
            return self.page(request, worker, form, None)
        if request.POST.get("action") != "generate":
            return self.page(request, worker, form, self.check(worker, form))
        if not consume_once_token(request):
            return self.already_submitted(
                request, reverse("workers:application_set", args=[worker.pk])
            )
        assert isinstance(request.user, User)
        try:
            document = form_generation.request(
                self.form_id,
                target=worker,
                data=self.data(form),
                basis_on=form.cleaned_data["basis_on"],
                by=request.user,
                accept_warnings=bool(form.cleaned_data.get("accept_warnings")),
            )
        except form_generation.GenerationBlocked as blocked:
            return self.page(request, worker, form, blocked.result)
        except form_templates.FormTemplateError:
            form.add_error(None, NOT_LOADED)
            return self.page(request, worker, form, None)
        except form_generation.WarningsNotAccepted as pending:
            form.add_error("accept_warnings", _("Check the warnings before generating."))
            return self.page(request, worker, form, pending.result)
        messages.success(request, _("Requested the generation of the form."))
        return redirect("forms:document", pk=document.pk)
