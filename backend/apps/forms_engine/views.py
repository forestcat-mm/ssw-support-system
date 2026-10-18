"""生成物の画面と、PDF の認可つきのダウンロード（T29・T31、ADR-0018）。

PDF のダウンロードはファイルの中身を渡す唯一の経路。記入済みの docx・xlsx は画面から取り出さない
（段階4 の F13 で決める）。
"""

from typing import Any

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
    ConsultationRecordForm,
    SignatureForm,
)
from apps.forms_engine.models import GeneratedDocument, GenerationStatus
from apps.hosts.models import HostOrganization
from apps.support.models import Briefing, DeliveryKind, PeriodicInterview, SupportPlanVersion
from apps.support.record_forms import ReasonForm
from apps.workers.views import WorkerDetailView, WorkerFormView
from services import (
    application_sets,
    attachments,
    audit,
    authorization,
    deliveries,
    form_data_consultation,
    form_generation,
    form_signatures,
    form_templates,
    workers,
)


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
        context.update(selector=form, overview=found, set_id=set_id, basis_on=on)
        return {
            "shown": {
                "application_set": set_id,
                "basis_on": on.isoformat(),
                "generated_documents": found.shown_documents,
            }
        }


def apply_errors(form: SignatureForm, error: ValidationError) -> None:
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
