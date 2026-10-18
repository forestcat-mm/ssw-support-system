"""雇用契約の登録・編集の業務画面（第5章 F03、T28b）。外国人詳細の「契約」タブから開く。

- 契約の登録（第1版）・新しい版・根拠資料の添付と取消・終了の登録は contracts.manage。
- 終了の取消は contracts.reopen（管理者）。理由は必須。
- 版は作った後に変えない。新しい版は前の版を写して開き、差分が無ければ保存しない（D4）。
- 無効化した外国人の契約は見るだけ（403）。編集の画面の表示は閲覧として記録しない。
"""

from __future__ import annotations

from typing import Any

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.contracts.forms import (
    AllowanceFormSet,
    DeductionFormSet,
    ShiftFormSet,
    TerminationForm,
    VersionAttachmentForm,
    VersionForm,
    rows_of,
)
from apps.contracts.models import EmploymentContract, EmploymentContractVersion
from apps.core.models import Action, Attachment, User
from apps.core.web_forms import consume_once_token
from apps.support.record_forms import ReasonForm
from apps.workers.models import ForeignWorker
from apps.workers.views import WorkerFormView
from services import attachments, authorization, contracts


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)
    return request.user


def tab_url(worker: ForeignWorker, contract: EmploymentContract | None = None) -> str:
    url = reverse("workers:contracts", args=[worker.pk])
    return f"{url}?contract={contract.pk}" if contract is not None else url


def apply_errors(form: forms.BaseForm, error: ValidationError) -> None:
    if not hasattr(error, "error_dict"):
        form.add_error(None, error)
        return
    for key, messages_ in error.error_dict.items():
        form.add_error(key if key in form.fields else None, messages_)


class ContractFormView(WorkerFormView):
    required_action = Action.CONTRACTS_MANAGE

    def contract(self, worker: ForeignWorker, contract_id: int) -> EmploymentContract:
        return get_object_or_404(worker.employment_contracts, pk=contract_id)


# --- 版（契約の登録・新しい版）------------------------------------------------------


class VersionEditor:
    """版のフォームと明細の行（交代制・手当・控除）の組。"""

    def __init__(
        self,
        data: Any = None,
        *,
        contract: EmploymentContract | None,
        previous: EmploymentContractVersion | None,
    ) -> None:
        initial = contracts.initial_from(previous) if previous is not None else {}
        rows = contracts.detail_initial(previous) if previous is not None else {}
        self.form = VersionForm(data, initial=initial, contract=contract)
        self.shifts = ShiftFormSet(data, prefix="shifts", initial=rows.get("shifts"))
        self.allowances = AllowanceFormSet(
            data, prefix="allowances", initial=rows.get("allowances")
        )
        self.deductions = DeductionFormSet(
            data, prefix="deductions", initial=rows.get("deductions")
        )

    def is_valid(self) -> bool:
        results = [
            self.form.is_valid(),
            self.shifts.is_valid(),
            self.allowances.is_valid(),
            self.deductions.is_valid(),
        ]
        return all(results)

    def details(self) -> contracts.Details:
        return contracts.Details(
            shifts=rows_of(self.shifts),
            allowances=rows_of(self.allowances),
            deductions=rows_of(self.deductions),
        )


def saved_messages(request: HttpRequest, saved: contracts.VersionSaved) -> None:
    messages.success(
        request, _("Saved the employment contract (version %(n)d).") % {"n": saved.version.version}
    )
    if saved.filing is not None:
        kind = saved.filing.get_kind_display()
        messages.info(
            request,
            _("Created the filing task: %(kind)s. Check whether the notification is required.")
            % {"kind": kind}
            if saved.filing.needs_review
            else _("Created the filing task: %(kind)s.") % {"kind": kind},
        )
    if saved.conversion_warning:
        messages.warning(request, saved.conversion_warning)
    if contracts.conversion_terms_missing(saved.version):
        messages.warning(
            request,
            _(
                "The working conditions change after conversion. Attach annex 2 of the "
                "employment conditions (form 1-6)."
            ),
        )


class VersionCreateView(ContractFormView):
    """契約の登録（contract_id なし。第1版）と、新しい版（前の版を写して開く）。"""

    template_name = "contracts/version_form.html"

    def target(self, request: HttpRequest, pk: int, contract_id: int | None) -> Any:
        worker = self.worker(request, pk)
        if contract_id is None:
            return worker, None, None
        contract = self.contract(worker, contract_id)
        if contract.terminated_on is not None:
            raise PermissionDenied  # 終了した契約には版を足さない（終了の取消の後に足す）
        return worker, contract, contracts.latest_version(contract)

    def page(
        self,
        request: HttpRequest,
        worker: ForeignWorker,
        contract: EmploymentContract | None,
        previous: EmploymentContractVersion | None,
        editor: VersionEditor,
    ) -> HttpResponse:
        return self.render(
            request,
            worker=worker,
            contract=contract,
            previous=previous,
            previous_attachments=list(previous.attachments.all()) if previous else [],
            editor=editor,
            back=tab_url(worker, contract),
        )

    def get(self, request: HttpRequest, pk: int, contract_id: int | None = None) -> HttpResponse:
        worker, contract, previous = self.target(request, pk, contract_id)
        editor = VersionEditor(contract=contract, previous=previous)
        return self.page(request, worker, contract, previous, editor)

    def post(self, request: HttpRequest, pk: int, contract_id: int | None = None) -> HttpResponse:
        worker, contract, previous = self.target(request, pk, contract_id)
        if not consume_once_token(request):
            return self.already_submitted(request, tab_url(worker, contract))
        editor = VersionEditor(request.POST, contract=contract, previous=previous)
        if editor.is_valid():
            version = editor.form.save(commit=False)
            try:
                saved = contracts.create_version(
                    version,
                    contract=contract,
                    worker=worker,
                    host_organization=version.site.host_organization,
                    categories=editor.form.cleaned_data["categories"],
                    details=editor.details(),
                    by=_actor(request),
                )
            except ValidationError as error:
                apply_errors(editor.form, error)
            else:
                saved_messages(request, saved)
                return redirect(tab_url(worker, saved.version.contract))
        return self.page(request, worker, contract, previous, editor)


# --- 根拠資料 ----------------------------------------------------------------------


class VersionAttachView(ContractFormView):
    template_name = "contracts/simple_form.html"

    def target(self, request: HttpRequest, pk: int, contract_id: int, number: int) -> Any:
        worker = self.worker(request, pk)
        contract = self.contract(worker, contract_id)
        version = get_object_or_404(contract.versions, version=number)
        return worker, contract, version

    def page(
        self, request: HttpRequest, worker: Any, contract: Any, version: Any, form: Any
    ) -> HttpResponse:
        return self.render(
            request,
            worker=worker,
            form=form,
            title=_("Attach documents to version %(n)d") % {"n": version.version},
            lead=_(
                "Evidence of equal pay, the calendar and agreement for variable working hours, "
                "or annex 2 (conditions after conversion). The version itself is not changed."
            ),
            back=tab_url(worker, contract),
            submit=_("Add"),
            multipart=True,
            form_id="version-attachment-form",
        )

    def get(self, request: HttpRequest, pk: int, contract_id: int, number: int) -> HttpResponse:
        worker, contract, version = self.target(request, pk, contract_id, number)
        return self.page(request, worker, contract, version, VersionAttachmentForm())

    def post(self, request: HttpRequest, pk: int, contract_id: int, number: int) -> HttpResponse:
        worker, contract, version = self.target(request, pk, contract_id, number)
        if not consume_once_token(request):
            return self.already_submitted(request, tab_url(worker, contract))
        form = VersionAttachmentForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                contracts.add_version_attachments(
                    version,
                    form.cleaned_data["kind"],
                    form.cleaned_data["uploads"],
                    by=_actor(request),
                )
            except ValidationError as error:
                apply_errors(form, error)
            else:
                messages.success(request, _("Added the attachments."))
                return redirect(tab_url(worker, contract))
        return self.page(request, worker, contract, version, form)


class VersionAttachmentVoidView(ContractFormView):
    template_name = "contracts/attachment_void.html"

    def target(self, request: HttpRequest, pk: int, contract_id: int, attachment_id: int) -> Any:
        worker = self.worker(request, pk)
        contract = self.contract(worker, contract_id)
        attachment = get_object_or_404(
            attachments.active(
                Attachment.objects.filter(
                    content_type__app_label="contracts",
                    content_type__model="employmentcontractversion",
                    object_id__in=list(contract.versions.values_list("pk", flat=True)),
                )
            ),
            pk=attachment_id,
        )
        return worker, contract, attachment

    def form(self, data: Any = None) -> ReasonForm:
        return ReasonForm(
            data, help_text=_("Example: a document of another contract was attached.")
        )

    def page(
        self, request: HttpRequest, worker: Any, contract: Any, attachment: Any, form: Any
    ) -> HttpResponse:
        return self.render(
            request,
            worker=worker,
            attachment=attachment,
            form=form,
            back=tab_url(worker, contract),
        )

    def get(
        self, request: HttpRequest, pk: int, contract_id: int, attachment_id: int
    ) -> HttpResponse:
        worker, contract, attachment = self.target(request, pk, contract_id, attachment_id)
        return self.page(request, worker, contract, attachment, self.form())

    def post(
        self, request: HttpRequest, pk: int, contract_id: int, attachment_id: int
    ) -> HttpResponse:
        worker, contract, attachment = self.target(request, pk, contract_id, attachment_id)
        if not consume_once_token(request):
            return self.already_submitted(request, tab_url(worker, contract))
        form = self.form(request.POST)
        if form.is_valid():
            attachments.void_attachment(
                attachment, by=_actor(request), reason=form.cleaned_data["reason"]
            )
            messages.success(request, _("Voided the attachment."))
            return redirect(tab_url(worker, contract))
        return self.page(request, worker, contract, attachment, form)


# --- 終了・終了の取消 ---------------------------------------------------------------


class TerminateView(ContractFormView):
    template_name = "contracts/simple_form.html"

    def target(self, request: HttpRequest, pk: int, contract_id: int) -> Any:
        worker = self.worker(request, pk)
        contract = self.contract(worker, contract_id)
        if contract.terminated_on is not None:
            raise PermissionDenied
        return worker, contract

    def page(self, request: HttpRequest, worker: Any, contract: Any, form: Any) -> HttpResponse:
        return self.render(
            request,
            worker=worker,
            form=form,
            title=_("Register the termination"),
            lead=_(
                "The notification of termination (within 14 days) is created on the dashboard. "
                "For dismissal or company reasons and other reasons, the notification of "
                "difficulty in acceptance is also created."
            ),
            back=tab_url(worker, contract),
            submit=_("Register the termination"),
            form_id="termination-form",
        )

    def get(self, request: HttpRequest, pk: int, contract_id: int) -> HttpResponse:
        worker, contract = self.target(request, pk, contract_id)
        return self.page(request, worker, contract, TerminationForm(instance=contract))

    def post(self, request: HttpRequest, pk: int, contract_id: int) -> HttpResponse:
        worker, contract = self.target(request, pk, contract_id)
        if not consume_once_token(request):
            return self.already_submitted(request, tab_url(worker, contract))
        form = TerminationForm(request.POST, instance=EmploymentContract(pk=contract.pk))
        if form.is_valid():
            try:
                contracts.terminate(
                    contract,
                    on=form.cleaned_data["terminated_on"],
                    reason=form.cleaned_data["termination_reason"],
                    note=form.cleaned_data.get("termination_note") or "",
                    by=_actor(request),
                )
            except ValidationError as error:
                apply_errors(form, error)
            else:
                messages.success(
                    request,
                    _("Registered the termination. The notification task was created."),
                )
                return redirect(tab_url(worker, contract))
        return self.page(request, worker, contract, form)


class ReopenView(ContractFormView):
    """終了の取消（入力ミスの訂正。管理者だけ。理由は必須）。"""

    template_name = "contracts/simple_form.html"
    required_action = Action.CONTRACTS_REOPEN

    def target(self, request: HttpRequest, pk: int, contract_id: int) -> Any:
        worker = self.worker(request, pk)
        contract = self.contract(worker, contract_id)
        if contract.terminated_on is None:
            raise PermissionDenied
        return worker, contract

    def form(self, data: Any = None) -> ReasonForm:
        form = ReasonForm(
            data, help_text=_("Example: the termination was registered for the wrong contract.")
        )
        form.fields["reason"].label = _("reason for cancelling the termination")
        return form

    def page(self, request: HttpRequest, worker: Any, contract: Any, form: Any) -> HttpResponse:
        return self.render(
            request,
            worker=worker,
            form=form,
            title=_("Cancel the termination"),
            lead=_(
                "Only for correcting a mistake. Unsubmitted termination notification tasks are "
                "cancelled with this reason."
            ),
            back=tab_url(worker, contract),
            submit=_("Cancel the termination"),
            form_id="reopen-form",
            danger=True,
        )

    def get(self, request: HttpRequest, pk: int, contract_id: int) -> HttpResponse:
        worker, contract = self.target(request, pk, contract_id)
        return self.page(request, worker, contract, self.form())

    def post(self, request: HttpRequest, pk: int, contract_id: int) -> HttpResponse:
        worker, contract = self.target(request, pk, contract_id)
        if not consume_once_token(request):
            return self.already_submitted(request, tab_url(worker, contract))
        form = self.form(request.POST)
        if form.is_valid():
            try:
                contracts.cancel_termination(
                    contract, by=_actor(request), reason=form.cleaned_data["reason"]
                )
            except ValidationError as error:
                apply_errors(form, error)
            else:
                messages.success(request, _("Cancelled the termination."))
                return redirect(tab_url(worker, contract))
        return self.page(request, worker, contract, form)


def can_reopen(user: Any) -> bool:
    return authorization.has_action(user, Action.CONTRACTS_REOPEN)
