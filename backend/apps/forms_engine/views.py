"""生成物の PDF の認可つきのダウンロード（T29、ADR-0018）。ファイルの中身を渡す唯一の経路。

記入済みの docx・xlsx は画面から取り出さない（段階4 の F13 で決める）。
"""

from django.core.exceptions import PermissionDenied
from django.http import FileResponse, HttpRequest
from django.shortcuts import get_object_or_404
from django.views import View

from apps.core.models import AuditAction
from apps.forms_engine.models import GeneratedDocument
from services import audit, form_generation


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
