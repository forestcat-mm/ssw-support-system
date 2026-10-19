"""受入れ機関・事業所の書類の保管庫の画面（受入れ機関の詳細から。T42 の追加）。"""

from typing import Any

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext as _

from apps.core.models import User
from apps.core.web_forms import consume_once_token
from apps.hosts.edit_views import HostFormView, detail_url
from apps.hosts.forms import HostDocumentForm, apply_errors
from apps.hosts.models import HostDocument, HostOrganization
from apps.support.record_forms import ReasonForm
from services import host_documents

ANCHOR = "host-documents"


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)
    return request.user


class HostDocumentAddView(HostFormView):
    """書類を足す（種類・事業所・対象期間または発行日・有効期限・言語・ファイル）。"""

    def page(self, request: HttpRequest, host: HostOrganization, form: Any) -> HttpResponse:
        return self.render(
            request,
            host=host,
            form=form,
            title=_("Add a document of the host organization"),
            lead=_(
                "The documents are chosen automatically for the documents for the application by "
                "the site of the employment contract and the basis date. The annual calendar must "
                "be written in Japanese and the language the worker fully understands."
            ),
            back=detail_url(host, ANCHOR),
            submit=_("Add"),
            form_id="host-document-form",
            multipart=True,
        )

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        host = self.host(request, pk)
        return self.page(request, host, HostDocumentForm(host=host))

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        host = self.host(request, pk)
        if not consume_once_token(request):
            return self.already_submitted(request, detail_url(host, ANCHOR))
        form = HostDocumentForm(request.POST, request.FILES, host=host)
        if form.is_valid():
            document = form.save(commit=False)
            document.host_organization = host
            try:
                host_documents.add(
                    document,
                    list(form.cleaned_data["uploads"]),
                    language_ids=[language.pk for language in form.cleaned_data["languages"]],
                    by=_actor(request),
                )
            except ValidationError as error:
                apply_errors(form, error)
            else:
                messages.success(request, _("Added the document."))
                return redirect(detail_url(host, ANCHOR))
        return self.page(request, host, form)


class HostDocumentVoidView(HostFormView):
    """書類を取り消す（理由は必須。ファイルも取り消す）。"""

    def target(
        self, request: HttpRequest, pk: int, document_id: int
    ) -> tuple[HostOrganization, HostDocument]:
        host = self.host(request, pk)
        document = get_object_or_404(host.documents.filter(voided_at__isnull=True), pk=document_id)
        return host, document

    def page(
        self, request: HttpRequest, host: HostOrganization, document: HostDocument, form: Any
    ) -> HttpResponse:
        return self.render(
            request,
            host=host,
            form=form,
            title=_("Void the document: %(kind)s") % {"kind": document.get_kind_display()},
            lead=_("The document and its files are kept and marked as voided."),
            back=detail_url(host, ANCHOR),
            submit=_("Void"),
            form_id="host-document-void-form",
        )

    def get(self, request: HttpRequest, pk: int, document_id: int) -> HttpResponse:
        host, document = self.target(request, pk, document_id)
        return self.page(request, host, document, ReasonForm())

    def post(self, request: HttpRequest, pk: int, document_id: int) -> HttpResponse:
        host, document = self.target(request, pk, document_id)
        if not consume_once_token(request):
            return self.already_submitted(request, detail_url(host, ANCHOR))
        form = ReasonForm(request.POST)
        if form.is_valid():
            try:
                host_documents.void(
                    document, by=_actor(request), reason=form.cleaned_data["reason"]
                )
            except ValidationError as error:
                form.add_error("reason", error)
            else:
                messages.success(request, _("Voided the document."))
                return redirect(detail_url(host, ANCHOR))
        return self.page(request, host, document, form)
