"""生成物の画面と、PDF の認可つきのダウンロード（T29・T31、ADR-0018）。

PDF のダウンロードはファイルの中身を渡す唯一の経路。記入済みの docx・xlsx は画面から取り出さない
（段階4 の F13 で決める）。
"""

from typing import Any

from django.core.exceptions import PermissionDenied
from django.http import FileResponse, HttpRequest
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import reverse
from django.views import View

from apps.core.models import Action, AuditAction
from apps.core.permissions import ActionRequiredMixin
from apps.forms_engine.models import GeneratedDocument, GenerationStatus
from apps.support.models import Briefing, DeliveryKind, SupportPlanVersion
from services import audit, authorization, form_generation, workers


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
        can_deliver = (
            version is not None
            and document.status == GenerationStatus.COMPLETED
            and document.worker is not None
            and workers.is_active(document.worker)
            and authorization.has_action(request.user, Action.SUPPORT_MANAGE)
        )
        if document.worker is not None:
            audit.record_view(document.worker, context={"generated_document": document.pk})
        back_url = ""
        if version is not None and document.worker is not None:
            back_url = (
                reverse("workers:support", args=[document.worker.pk])
                + f"?plan={version.plan_id}&version={version.version}"
            )
        elif isinstance(target, Briefing):
            back_url = reverse("workers:support_briefing", args=[target.worker_id, target.pk])
        elif document.worker is not None:
            back_url = reverse("workers:detail", args=[document.worker.pk])
        context: dict[str, Any] = {
            "document": document,
            "worker": document.worker,
            "plan_version": version,
            "back_url": back_url,
            "can_download": form_generation.can_download_pdf(request.user, document),
            "can_deliver": can_deliver,
            "delivery_kind": DeliveryKind.SUPPORT_PLAN_COPY,
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
