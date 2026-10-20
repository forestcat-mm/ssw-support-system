"""受入れ機関の一括申請の項目の画面（決算・申告の版・役員・申請担当者。T46b、ADR-0038）。

操作は hosts.manage。表示は受入れ機関の詳細の「一括申請の項目」。
"""

from dataclasses import dataclass
from typing import Any

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.functional import Promise
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.core.web_forms import consume_once_token
from apps.hosts.application_forms import (
    ApplicantContactForm,
    DeclarationForm,
    FinancialYearForm,
    OfficerForm,
)
from apps.hosts.edit_views import HostFormView, _actor, detail_url
from apps.hosts.models import (
    HostApplicantContact,
    HostDeclaration,
    HostFinancialYear,
    HostOfficer,
    HostOrganization,
)
from apps.support.record_forms import ReasonForm
from services import host_application, host_declarations

SECTION = "host-application"


@dataclass(frozen=True)
class RowKind:
    model: type[Any]
    form: type[forms.ModelForm[Any]]
    add_title: str | Promise
    edit_title: str | Promise
    editable: bool  # 編集できる（決算は取消して入れ直す）
    voidable: bool


ROW_KINDS: dict[str, RowKind] = {
    "financial-years": RowKind(
        HostFinancialYear,
        FinancialYearForm,
        gettext_lazy("Add financial results"),
        gettext_lazy("Financial results"),
        editable=False,
        voidable=True,
    ),
    "officers": RowKind(
        HostOfficer,
        OfficerForm,
        gettext_lazy("Add an officer"),
        gettext_lazy("Edit the officer"),
        editable=True,
        voidable=True,
    ),
    "applicant-contacts": RowKind(
        HostApplicantContact,
        ApplicantContactForm,
        gettext_lazy("Add an applicant contact"),
        gettext_lazy("Edit the applicant contact"),
        editable=True,
        voidable=False,
    ),
}


def _kind(kind: str) -> RowKind:
    if kind not in ROW_KINDS:
        raise Http404
    return ROW_KINDS[kind]


class HostRowEditView(HostFormView):
    """決算・役員・申請担当者の追加（row_id なし）と編集。"""

    def target(self, request: HttpRequest, pk: int, kind: str, row_id: int | None) -> Any:
        host = self.host(request, pk)
        config = _kind(kind)
        if row_id is None:
            return host, config, None
        if not config.editable:
            raise Http404
        row = get_object_or_404(config.model, pk=row_id, host_organization=host)
        if getattr(row, "voided_at", None) is not None:
            raise Http404
        return host, config, row

    def page(
        self, request: HttpRequest, host: HostOrganization, config: RowKind, row: Any, form: Any
    ) -> HttpResponse:
        return self.render(
            request,
            host=host,
            form=form,
            title=config.edit_title if row else config.add_title,
            lead=_("Items of the bulk application tool of the online residence application."),
            back=detail_url(host, SECTION),
            submit=_("Save"),
            form_id="host-row-form",
        )

    def get(
        self, request: HttpRequest, pk: int, kind: str, row_id: int | None = None
    ) -> HttpResponse:
        host, config, row = self.target(request, pk, kind, row_id)
        return self.page(request, host, config, row, config.form(instance=row))

    def post(
        self, request: HttpRequest, pk: int, kind: str, row_id: int | None = None
    ) -> HttpResponse:
        host, config, row = self.target(request, pk, kind, row_id)
        if not consume_once_token(request):
            return self.already_submitted(request, detail_url(host, SECTION))
        form = config.form(request.POST, instance=row)
        if not form.is_valid():
            return self.page(request, host, config, row, form)
        record = form.save(commit=False)
        record.host_organization = host
        host_application.save_row(record, by=_actor(request))
        messages.success(request, _("Saved."))
        return redirect(detail_url(host, SECTION))


class HostRowVoidView(HostFormView):
    """決算・役員の行の取消（理由は必須）。"""

    template_name = "hosts/row_void.html"

    def target(self, request: HttpRequest, pk: int, kind: str, row_id: int) -> Any:
        host = self.host(request, pk)
        config = _kind(kind)
        if not config.voidable:
            raise Http404
        row = get_object_or_404(
            config.model, pk=row_id, host_organization=host, voided_at__isnull=True
        )
        return host, row

    def page(
        self, request: HttpRequest, host: HostOrganization, row: Any, form: Any
    ) -> HttpResponse:
        return self.render(request, host=host, row=row, form=form, back=detail_url(host, SECTION))

    def get(self, request: HttpRequest, pk: int, kind: str, row_id: int) -> HttpResponse:
        host, row = self.target(request, pk, kind, row_id)
        return self.page(request, host, row, ReasonForm())

    def post(self, request: HttpRequest, pk: int, kind: str, row_id: int) -> HttpResponse:
        host, row = self.target(request, pk, kind, row_id)
        if not consume_once_token(request):
            return self.already_submitted(request, detail_url(host, SECTION))
        form = ReasonForm(request.POST)
        if form.is_valid():
            try:
                host_application.void_row(
                    row, by=_actor(request), reason=form.cleaned_data["reason"]
                )
            except ValidationError as error:
                form.add_error("reason", error)
            else:
                messages.success(request, _("Voided the record."))
                return redirect(detail_url(host, SECTION))
        return self.page(request, host, row, form)


class DeclarationCreateView(HostFormView):
    """申告（区分V (11)〜(32)・第1-11-1号 3 の人数）の新しい版。前の版を初期値にする。"""

    template_name = "hosts/declaration_form.html"

    def page(self, request: HttpRequest, host: HostOrganization, form: Any) -> HttpResponse:
        return self.render(request, host=host, form=form, back=detail_url(host, SECTION))

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        host = self.host(request, pk)
        return self.page(request, host, DeclarationForm(previous=host_declarations.latest(host)))

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        host = self.host(request, pk)
        if not consume_once_token(request):
            return self.already_submitted(request, detail_url(host, SECTION))
        form = DeclarationForm(
            request.POST,
            instance=HostDeclaration(host_organization=host),
            previous=host_declarations.latest(host),
        )
        if not form.is_valid():
            return self.page(request, host, form)
        host_application.add_declaration(
            form.save(commit=False), form.answers(), by=_actor(request)
        )
        messages.success(request, _("Added a new version of the declaration."))
        return redirect(detail_url(host, SECTION))


class DeclarationVoidView(HostRowVoidView):
    """申告の版の取消（前の版が最新に戻る）。"""

    def target(self, request: HttpRequest, pk: int, kind: str, row_id: int) -> Any:
        host = self.host(request, pk)
        row = get_object_or_404(
            HostDeclaration, pk=row_id, host_organization=host, voided_at__isnull=True
        )
        return host, row
