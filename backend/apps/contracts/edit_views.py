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
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy, ngettext

from apps.contracts.forms import (
    AllowanceFormSet,
    ContractGenerateForm,
    DeductionFormSet,
    ShiftFormSet,
    TerminationForm,
    VersionAttachmentForm,
    VersionForm,
    WageExplanationForm,
    rows_of,
)
from apps.contracts.models import EmploymentContract, EmploymentContractVersion, WageUnit
from apps.core.models import Action, Attachment, User
from apps.core.web_forms import consume_once_token
from apps.forms_engine.forms import NOT_LOADED
from apps.support.record_forms import ReasonForm
from apps.workers.models import ForeignWorker
from apps.workers.views import WorkerFormView
from services import (
    attachments,
    authorization,
    contracts,
    form_checks,
    form_data_application,
    form_data_career,
    form_data_contract,
    form_generation,
    form_languages,
    form_templates,
    wage_explanations,
)


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
                "Only for correcting a mistake. Notification tasks not yet started are cancelled "
                "with this reason. Tasks in progress are kept and marked; submitted or closed "
                "tasks get a note."
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
                left = contracts.cancel_termination(
                    contract, by=_actor(request), reason=form.cleaned_data["reason"]
                )
            except ValidationError as error:
                apply_errors(form, error)
            else:
                messages.success(request, _("Cancelled the termination."))
                if left:
                    messages.warning(
                        request,
                        ngettext(
                            "%(count)d notification task is in progress and was kept. Check it.",
                            "%(count)d notification tasks are in progress and were kept. "
                            "Check them.",
                            len(left),
                        )
                        % {"count": len(left)},
                    )
                return redirect(tab_url(worker, contract))
        return self.page(request, worker, contract, form)


def can_reopen(user: Any) -> bool:
    return authorization.has_action(user, Action.CONTRACTS_REOPEN)


# --- 報酬の説明（第1-4号の元データ。T34-D2）---------------------------------------------


class WageExplanationView(ContractFormView):
    """報酬の説明の入力・直し（版と 1 対 1。版と違って直せる。作成・更新は監査ログに残る）。"""

    template_name = "contracts/wage_explanation_form.html"

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
            contract=contract,
            version=version,
            form=form,
            applicant_monthly=wage_explanations.applicant_wage(version, WageUnit.MONTHLY),
            applicant_hourly=wage_explanations.applicant_wage(version, WageUnit.HOURLY),
            back=tab_url(worker, contract) + f"&version={version.version}",
        )

    def get(self, request: HttpRequest, pk: int, contract_id: int, number: int) -> HttpResponse:
        worker, contract, version = self.target(request, pk, contract_id, number)
        explanation = wage_explanations.explanation_of(version)
        form = (
            WageExplanationForm(instance=explanation)
            if explanation is not None
            else WageExplanationForm(initial=wage_explanations.initial_values(version))
        )
        return self.page(request, worker, contract, version, form)

    def post(self, request: HttpRequest, pk: int, contract_id: int, number: int) -> HttpResponse:
        worker, contract, version = self.target(request, pk, contract_id, number)
        if not consume_once_token(request):
            return self.already_submitted(request, tab_url(worker, contract))
        form = WageExplanationForm(request.POST, instance=wage_explanations.explanation_of(version))
        if form.is_valid():
            try:
                wage_explanations.save(version, form.cleaned_data, by=_actor(request))
            except ValidationError as error:
                apply_errors(form, error)
            else:
                messages.success(request, _("Saved the wage explanation."))
                return redirect(tab_url(worker, contract) + f"&version={version.version}")
        return self.page(request, worker, contract, version, form)


# --- 雇用契約の書類の生成（第1-4号・第1-5号・第1-6号。T34、ADR-0023）------------------------

CONTRACT_FORMS = {
    "1-6": gettext_lazy("employment conditions (form 1-6)"),
    "1-5": gettext_lazy("employment contract (form 1-5)"),
    "1-4": gettext_lazy("explanation of wages (form 1-4)"),
}


class ContractGenerateView(WorkerFormView):
    """契約タブの版から生成する（対象は版、基準日は画面で入れる。T34-D9）。

    第1-4号は作成責任者を入れる（初期値は受入れ機関の代表者。変えた値はスナップショットに残る）。
    """

    required_action = Action.FORMS_GENERATE
    template_name = "contracts/generate.html"

    def target(
        self, request: HttpRequest, pk: int, contract_id: int, number: int, form_id: str
    ) -> tuple[ForeignWorker, EmploymentContractVersion]:
        if form_id not in CONTRACT_FORMS:
            raise Http404
        worker = self.worker(request, pk)
        contract = get_object_or_404(worker.employment_contracts, pk=contract_id)
        return worker, get_object_or_404(contract.versions, version=number)

    def data(
        self, version: EmploymentContractVersion, form_id: str, form: ContractGenerateForm
    ) -> dict[str, Any]:
        if form_id == "1-6":
            return form_data_contract.build_1_6(version)
        if form_id == "1-5":
            return form_data_contract.build_1_5(version)
        return form_data_contract.build_1_4(
            version, form.cleaned_data["basis_on"], author=form.cleaned_data.get("author", "")
        )

    def page(
        self,
        request: HttpRequest,
        worker: ForeignWorker,
        version: EmploymentContractVersion,
        form_id: str,
        form: ContractGenerateForm,
        result: form_checks.CheckResult | None,
    ) -> HttpResponse:
        back = tab_url(worker, version.contract) + f"&version={version.version}"
        return self.render(
            request,
            worker=worker,
            version=version,
            form_id=form_id,
            form_name=CONTRACT_FORMS[form_id],
            form=form,
            bilingual_notice=form_languages.outdated_notice(worker, form_id),
            result=result,
            back_url=back,
        )

    def check(
        self, version: EmploymentContractVersion, form_id: str, form: ContractGenerateForm
    ) -> form_checks.CheckResult | None:
        try:
            return form_generation.check(
                form_id,
                target=version,
                data=self.data(version, form_id, form),
                basis_on=form.cleaned_data["basis_on"],
                language=form.chosen_language,
            )
        except form_templates.FormTemplateError:
            form.add_error(None, NOT_LOADED)
            return None

    def get(
        self, request: HttpRequest, pk: int, contract_id: int, number: int, form_id: str
    ) -> HttpResponse:
        worker, version = self.target(request, pk, contract_id, number, form_id)
        initial = {
            "basis_on": timezone.localdate().isoformat(),
            "author": form_data_contract.default_author(version),
            "language": form_languages.default_for(worker, form_id),
        }
        form = self.form_class(form_id, initial)
        result = self.check(version, form_id, form) if form.is_valid() else None
        form = self.form_class(form_id, initial=initial)
        return self.page(request, worker, version, form_id, form, result)

    def form_class(self, form_id: str, *args: Any, **kwargs: Any) -> ContractGenerateForm:
        return ContractGenerateForm(
            *args,
            with_author=form_id == "1-4",
            languages=form_languages.choices(form_id),
            **kwargs,
        )

    def post(
        self, request: HttpRequest, pk: int, contract_id: int, number: int, form_id: str
    ) -> HttpResponse:
        worker, version = self.target(request, pk, contract_id, number, form_id)
        form = self.form_class(form_id, request.POST)
        if not form.is_valid():
            return self.page(request, worker, version, form_id, form, None)
        if request.POST.get("action") != "generate":
            result = self.check(version, form_id, form)
            return self.page(request, worker, version, form_id, form, result)
        back = tab_url(worker, version.contract)
        if not consume_once_token(request):
            return self.already_submitted(request, back)
        try:
            document = form_generation.request(
                form_id,
                target=version,
                data=self.data(version, form_id, form),
                basis_on=form.cleaned_data["basis_on"],
                by=_actor(request),
                language=form.chosen_language,
                accept_warnings=bool(form.cleaned_data.get("accept_warnings")),
            )
        except form_generation.GenerationBlocked as blocked:
            return self.page(request, worker, version, form_id, form, blocked.result)
        except form_templates.FormTemplateError:
            form.add_error(None, NOT_LOADED)
            return self.page(request, worker, version, form_id, form, None)
        except form_generation.WarningsNotAccepted as pending:
            form.add_error("accept_warnings", _("Check the warnings before generating."))
            return self.page(request, worker, version, form_id, form, pending.result)
        messages.success(request, _("Requested the generation of the form."))
        return redirect("forms:document", pk=document.pk)


# --- 契約を対象にする書類（自機関の書類〔T36、ADR-0025〕と申請の書類〔T37、ADR-0026〕）---------

OWN_FORMS = {
    "career-plan": gettext_lazy("career-up plan"),
    "experience-certificate": gettext_lazy("certificate of work experience"),
    "3-1": gettext_lazy("pledge of the industrial products manufacturing field (form 3-1)"),
    "13-1": gettext_lazy("pledge of the food manufacturing field by the host (form 13-1)"),
    "13-2": gettext_lazy("pledge of the food manufacturing field by us (form 13-2)"),
    "5-10": gettext_lazy("support entrustment contract (form 5-10)"),
    "1-25": gettext_lazy("explanation of the support entrustment contract (form 1-25)"),
}
# 作成責任者を画面で入れる様式（初期値は form_data_application.default_author。T37-D10）
AUTHOR_FORMS = frozenset({"3-1", "13-1", "13-2", "1-25"})
APPLICATION_FORMS = frozenset({"3-1", "13-1", "13-2", "5-10", "1-25"})


class OwnDocumentGenerateView(WorkerFormView):
    """契約を対象に書類を生成する（基準日は画面で入れる。T36-D2）。

    申請の書類（第3-1号・第13-1号・第13-2号・第1-25号）は作成責任者も入れる。初期値は受入れ機関の
    代表者（第13-2号は自機関の代表者）で、変えた値はデータとしてスナップショットに残る（T37-D10）。
    """

    required_action = Action.FORMS_GENERATE
    template_name = "contracts/generate.html"

    def target(
        self, request: HttpRequest, pk: int, contract_id: int, form_id: str
    ) -> tuple[ForeignWorker, EmploymentContract]:
        if form_id not in OWN_FORMS:
            raise Http404
        worker = self.worker(request, pk)
        return worker, get_object_or_404(worker.employment_contracts, pk=contract_id)

    def data(
        self, contract: EmploymentContract, form_id: str, form: ContractGenerateForm
    ) -> dict[str, Any]:
        on = form.cleaned_data["basis_on"]
        if form_id in APPLICATION_FORMS:
            author = form.cleaned_data.get("author", "") or ""
            return form_data_application.build(form_id, contract, on, author=author.strip())
        if form_id == "career-plan":
            return form_data_career.build_career_plan(contract, on)
        return form_data_career.build_experience_certificate(contract, on)

    def page(
        self,
        request: HttpRequest,
        worker: ForeignWorker,
        contract: EmploymentContract,
        form_id: str,
        form: ContractGenerateForm,
        result: form_checks.CheckResult | None,
    ) -> HttpResponse:
        return self.render(
            request,
            worker=worker,
            version=contracts.latest_version(contract),
            form_id=form_id,
            form_name=OWN_FORMS[form_id],
            form=form,
            result=result,
            back_url=tab_url(worker, contract),
        )

    def check(
        self, contract: EmploymentContract, form_id: str, form: ContractGenerateForm
    ) -> form_checks.CheckResult | None:
        on = form.cleaned_data["basis_on"]
        try:
            return form_generation.check(
                form_id, target=contract, data=self.data(contract, form_id, form), basis_on=on
            )
        except form_templates.FormTemplateError:
            form.add_error(None, NOT_LOADED)
            return None

    def form_class(self, form_id: str, *args: Any, **kwargs: Any) -> ContractGenerateForm:
        if form_id not in AUTHOR_FORMS:
            return ContractGenerateForm(*args, **kwargs)
        return ContractGenerateForm(
            *args,
            with_author=True,
            author_help=_("Printed in the form. The default is the representative."),
            **kwargs,
        )

    def get(self, request: HttpRequest, pk: int, contract_id: int, form_id: str) -> HttpResponse:
        worker, contract = self.target(request, pk, contract_id, form_id)
        initial = {"basis_on": timezone.localdate().isoformat()}
        if form_id in AUTHOR_FORMS:
            initial["author"] = form_data_application.default_author(form_id, contract)
        form = self.form_class(form_id, initial)
        result = self.check(contract, form_id, form) if form.is_valid() else None
        form = self.form_class(form_id, initial=initial)
        return self.page(request, worker, contract, form_id, form, result)

    def post(self, request: HttpRequest, pk: int, contract_id: int, form_id: str) -> HttpResponse:
        worker, contract = self.target(request, pk, contract_id, form_id)
        form = self.form_class(form_id, request.POST)
        if not form.is_valid():
            return self.page(request, worker, contract, form_id, form, None)
        if request.POST.get("action") != "generate":
            result = self.check(contract, form_id, form)
            return self.page(request, worker, contract, form_id, form, result)
        if not consume_once_token(request):
            return self.already_submitted(request, tab_url(worker, contract))
        on = form.cleaned_data["basis_on"]
        try:
            document = form_generation.request(
                form_id,
                target=contract,
                data=self.data(contract, form_id, form),
                basis_on=on,
                by=_actor(request),
                accept_warnings=bool(form.cleaned_data.get("accept_warnings")),
            )
        except form_generation.GenerationBlocked as blocked:
            return self.page(request, worker, contract, form_id, form, blocked.result)
        except form_templates.FormTemplateError:
            form.add_error(None, NOT_LOADED)
            return self.page(request, worker, contract, form_id, form, None)
        except form_generation.WarningsNotAccepted as pending:
            form.add_error("accept_warnings", _("Check the warnings before generating."))
            return self.page(request, worker, contract, form_id, form, pending.result)
        messages.success(request, _("Requested the generation of the form."))
        return redirect("forms:document", pk=document.pk)
