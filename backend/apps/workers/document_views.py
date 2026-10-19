"""外国人の健康診断の書類（要配慮の添付）の画面（T23、ADR-0011）。「基本」タブの区分から開く。

アップロードと取消は workers.manage。ダウンロードは AttachmentDownloadView（can_download）だけ。
要配慮の添付のファイル名は、ダウンロードできる人にだけ見せる（通知文にも出さない）。
"""

from __future__ import annotations

from typing import Any

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.core.models import Attachment, User
from apps.core.web_forms import StyledFormMixin, consume_once_token
from apps.support.record_forms import MultipleFileField, ReasonForm
from apps.workers.models import ForeignWorker
from apps.workers.views import WorkerFormView
from services import attachments, worker_documents


def basic_url(worker: ForeignWorker) -> str:
    return reverse("workers:detail", args=[worker.pk]) + "#health-documents"


class HealthDocumentForm(StyledFormMixin, forms.Form):
    kind = forms.ChoiceField(
        label=gettext_lazy("kind"),
        choices=[(kind.value, kind.label) for kind in worker_documents.HEALTH_KINDS],
    )
    uploads = MultipleFileField(
        label=gettext_lazy("files"),
        help_text=gettext_lazy(
            "PDF, PNG or JPEG, up to 10 MB each. Attach the Japanese translation together."
        ),
    )
    examined_on = forms.DateField(
        label=gettext_lazy("examined on"),
        widget=forms.DateInput,
        help_text=gettext_lazy(
            "The date of the health check. Only the date and the files are kept; the findings are "
            "not entered (sensitive personal information)."
        ),
    )
    retention_until = forms.DateField(
        label=gettext_lazy("keep until (guide)"),
        required=False,
        widget=forms.DateInput,
        help_text=gettext_lazy(
            "Optional. A guide for when to delete it, decided in operation. "
            "It is not deleted automatically."
        ),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()

    def apply_errors(self, error: ValidationError) -> None:
        for key, messages_ in error.error_dict.items():
            name = "uploads" if key == "files" else key
            self.add_error(name if name in self.fields else None, messages_)


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)
    return request.user


class HealthDocumentAddView(WorkerFormView):
    """健康診断の書類を添付する（種別ごと。複数のファイル可）。"""

    template_name = "workers/health_document_form.html"

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        worker = self.worker(request, pk)
        kind = request.GET.get("kind")
        initial = {"kind": kind} if kind in {k.value for k in worker_documents.HEALTH_KINDS} else {}
        return self.render(request, worker=worker, form=HealthDocumentForm(initial=initial))

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        worker = self.worker(request, pk)
        if not consume_once_token(request):
            return self.already_submitted(request, basic_url(worker))
        form = HealthDocumentForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                worker_documents.add_documents(
                    worker,
                    form.cleaned_data["kind"],
                    form.cleaned_data["uploads"],
                    retention_until=form.cleaned_data["retention_until"],
                    examined_on=form.cleaned_data["examined_on"],
                    by=_actor(request),
                )
            except ValidationError as error:
                form.apply_errors(error)
            else:
                messages.success(request, _("Added the health check documents."))
                return redirect(basic_url(worker))
        return self.render(request, worker=worker, form=form)


class HealthDocumentVoidView(WorkerFormView):
    """健康診断の書類の取消（理由は必須。ファイルは消さずに打ち消して残す）。"""

    template_name = "workers/health_document_void.html"

    def target(self, request: HttpRequest, pk: int, attachment_id: int) -> Any:
        worker = self.worker(request, pk)
        attachment = get_object_or_404(
            attachments.active(worker_documents.documents_of(worker)), pk=attachment_id
        )
        return worker, attachment

    def form(self, data: Any = None) -> ReasonForm:
        return ReasonForm(data, help_text=_("Example: a document of another person was attached."))

    def page(
        self, request: HttpRequest, worker: Any, attachment: Attachment, form: Any
    ) -> HttpResponse:
        return self.render(
            request,
            worker=worker,
            attachment=attachment,
            can_download=attachments.can_download(request.user, attachment),
            form=form,
        )

    def get(self, request: HttpRequest, pk: int, attachment_id: int) -> HttpResponse:
        worker, attachment = self.target(request, pk, attachment_id)
        return self.page(request, worker, attachment, self.form())

    def post(self, request: HttpRequest, pk: int, attachment_id: int) -> HttpResponse:
        worker, attachment = self.target(request, pk, attachment_id)
        if not consume_once_token(request):
            return self.already_submitted(request, basic_url(worker))
        form = self.form(request.POST)
        if form.is_valid():
            try:
                attachments.void_attachment(
                    attachment, by=_actor(request), reason=form.cleaned_data["reason"]
                )
            except ValidationError as error:
                form.add_error("reason", error)
            else:
                messages.success(request, _("Voided the attachment."))
                return redirect(basic_url(worker))
        return self.page(request, worker, attachment, form)
