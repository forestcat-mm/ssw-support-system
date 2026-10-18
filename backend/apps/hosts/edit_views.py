"""受入れ機関の登録・編集の業務画面（第5章 F02、T28a）。受入れ機関の一覧・詳細から開く。

操作はすべて hosts.manage。保存・期限のタスク・添付は services.host_records（検査は
services.hosts）。
編集の画面の表示は閲覧として記録しない（外国人の編集の画面と同じ。閲覧は詳細の画面で 1 件）。
"""

from __future__ import annotations

from typing import Any

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import Promise
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy, ngettext
from django.views import View

from apps.core.models import Action, Attachment, User
from apps.core.permissions import ActionRequiredMixin
from apps.core.web_forms import consume_once_token, once_context
from apps.hosts.forms import (
    CertificateForm,
    CooperationForm,
    HostOrganizationForm,
    HostSiteForm,
    MembershipForm,
    SupportContractForm,
    SupportFeeFormSet,
    SupportTerminationForm,
    apply_errors,
)
from apps.hosts.models import (
    HostCooperationConfirmation,
    HostMembership,
    HostOrganization,
    HostSite,
    SupportContract,
)
from apps.support.record_forms import AttachmentForm, ReasonForm
from services import attachments, authorization, filings, host_records, support_filings


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)
    return request.user


def detail_url(host: HostOrganization, section: str = "") -> str:
    return reverse("hosts:detail", args=[host.pk]) + (f"#{section}" if section else "")


def sync_messages(request: HttpRequest, result: filings.SyncResult) -> None:
    """期限のタスクの処理の結果を知らせる（自動で完了・取消にしたもの、動かさなかったもの）。"""
    if result.created:
        messages.info(
            request,
            ngettext(
                "Created %(count)d task for the deadline on the dashboard.",
                "Created %(count)d tasks for the deadline on the dashboard.",
                len(result.created),
            )
            % {"count": len(result.created)},
        )
    if result.closed:
        messages.info(
            request,
            ngettext(
                "Closed %(count)d task of the previous deadline automatically "
                "(the reason is kept in the task).",
                "Closed %(count)d tasks of the previous deadline automatically "
                "(the reason is kept in the task).",
                len(result.closed),
            )
            % {"count": len(result.closed)},
        )
    if result.left_open:
        messages.warning(
            request,
            ngettext(
                "%(count)d task of the previous deadline is in progress and was not changed. "
                "Check it on the dashboard.",
                "%(count)d tasks of the previous deadline are in progress and were not changed. "
                "Check them on the dashboard.",
                len(result.left_open),
            )
            % {"count": len(result.left_open)},
        )


def raised_messages(request: HttpRequest, raised: support_filings.Raised) -> None:
    """支援委託契約の届出タスクの起票・取消の結果を知らせる。"""
    if raised.created:
        names = "、".join(str(worker) for worker in raised.workers)
        messages.info(
            request,
            ngettext(
                "Created %(count)d notification task for: %(names)s.",
                "Created %(count)d notification tasks for: %(names)s.",
                len(raised.created),
            )
            % {"count": len(raised.created), "names": names},
        )
    if raised.left_open:
        messages.warning(
            request,
            ngettext(
                "%(count)d notification task is in progress and was kept. Check it.",
                "%(count)d notification tasks are in progress and were kept. Check them.",
                len(raised.left_open),
            )
            % {"count": len(raised.left_open)},
        )


class HostFormView(ActionRequiredMixin, View):
    """登録・編集の画面の共通部分（対象の受入れ機関・二重送信の防止・表示）。"""

    required_action = Action.HOSTS_MANAGE
    template_name = "hosts/form.html"

    def host(self, request: HttpRequest, pk: int) -> HostOrganization:
        queryset = authorization.scope(request.user, HostOrganization.objects.all())
        return get_object_or_404(queryset, pk=pk)

    def render(self, request: HttpRequest, **context: Any) -> HttpResponse:
        return TemplateResponse(request, self.template_name, {**context, **once_context(request)})

    def already_submitted(self, request: HttpRequest, redirect_to: str) -> HttpResponse:
        messages.info(request, _("This form has already been submitted."))
        return redirect(redirect_to)


# --- 受入れ機関 --------------------------------------------------------------------


class HostEditView(HostFormView):
    """受入れ機関の登録（pk なし）と編集（法人情報・代表者・連絡先）。"""

    def page(
        self, request: HttpRequest, host: HostOrganization | None, form: forms.BaseForm
    ) -> HttpResponse:
        return self.render(
            request,
            host=host,
            form=form,
            title=_("Edit the host organization") if host else _("Register a host organization"),
            lead=_(
                "Corporate information used for the support plan and employment documents. "
                "After registering, add sites, memberships, cooperation confirmations and the "
                "support contract on the detail page."
            ),
            back=detail_url(host, "host-basic") if host else reverse("hosts:list"),
            submit=_("Save"),
            form_id="host-form",
        )

    def get(self, request: HttpRequest, pk: int | None = None) -> HttpResponse:
        host = self.host(request, pk) if pk is not None else None
        return self.page(request, host, HostOrganizationForm(instance=host))

    def post(self, request: HttpRequest, pk: int | None = None) -> HttpResponse:
        host = self.host(request, pk) if pk is not None else None
        back = detail_url(host) if host else reverse("hosts:list")
        if not consume_once_token(request):
            return self.already_submitted(request, back)
        form = HostOrganizationForm(request.POST, instance=host)
        if form.is_valid():
            try:
                saved = host_records.save_host(form.save(commit=False), by=_actor(request))
            except ValidationError as error:
                apply_errors(form, error)
            else:
                messages.success(request, _("Saved."))
                return redirect(detail_url(saved, "host-basic"))
        return self.page(request, host, form)


# --- 事業所 -----------------------------------------------------------------------


class SiteEditView(HostFormView):
    def target(self, request: HttpRequest, pk: int, site_id: int | None) -> Any:
        host = self.host(request, pk)
        site = get_object_or_404(host.sites, pk=site_id) if site_id is not None else None
        return host, site

    def page(
        self, request: HttpRequest, host: HostOrganization, site: HostSite | None, form: Any
    ) -> HttpResponse:
        return self.render(
            request,
            host=host,
            form=form,
            title=_("Edit the site") if site else _("Add a site"),
            lead=_(
                "A site is the workplace of employment contracts. If employment contracts use "
                "the site, its field cannot be changed. The categories used by the versions in "
                "effect today or to be applied later cannot be removed, and the site cannot be "
                "deactivated while those versions use it."
            ),
            back=detail_url(host, "sites"),
            submit=_("Save"),
            form_id="site-form",
        )

    def get(self, request: HttpRequest, pk: int, site_id: int | None = None) -> HttpResponse:
        host, site = self.target(request, pk, site_id)
        return self.page(request, host, site, HostSiteForm(instance=site))

    def post(self, request: HttpRequest, pk: int, site_id: int | None = None) -> HttpResponse:
        host, site = self.target(request, pk, site_id)
        if not consume_once_token(request):
            return self.already_submitted(request, detail_url(host, "sites"))
        form = HostSiteForm(request.POST, instance=site)
        if form.is_valid():
            record = form.save(commit=False)
            record.host_organization = host
            try:
                saved = host_records.save_site(
                    record,
                    form.cleaned_data["categories"],
                    by=_actor(request),
                    today=timezone.localdate(),
                )
            except ValidationError as error:
                apply_errors(form, error)
            else:
                messages.success(request, _("Saved."))
                if saved.resubmission_needed:
                    messages.warning(
                        request,
                        _(
                            "The address of the site was changed. The cooperation confirmation "
                            "must be submitted again to the municipalities. After submitting, "
                            "register it as a new submission."
                        ),
                    )
                return redirect(detail_url(host, "sites"))
        return self.page(request, host, site, form)


# --- 加入状況 ---------------------------------------------------------------------


class MembershipEditView(HostFormView):
    def target(self, request: HttpRequest, pk: int, membership_id: int | None) -> Any:
        host = self.host(request, pk)
        membership = (
            get_object_or_404(host.memberships, pk=membership_id)
            if membership_id is not None
            else None
        )
        return host, membership

    def page(
        self,
        request: HttpRequest,
        host: HostOrganization,
        membership: HostMembership | None,
        form: Any,
    ) -> HttpResponse:
        return self.render(
            request,
            host=host,
            form=form,
            title=(
                _("Edit the membership: %(body)s") % {"body": membership.field.membership_body_name}
                if membership
                else _("Add a membership")
            ),
            lead=_(
                "Membership of the registered corporation (industrial products) or the council "
                "(food manufacturing), one per field. To replace the certificate, use "
                "“Replace” in the row on the detail page."
            ),
            back=detail_url(host, "memberships"),
            submit=_("Save"),
            form_id="membership-form",
            multipart=membership is None,
        )

    def get(self, request: HttpRequest, pk: int, membership_id: int | None = None) -> HttpResponse:
        host, membership = self.target(request, pk, membership_id)
        return self.page(request, host, membership, MembershipForm(instance=membership, host=host))

    def post(self, request: HttpRequest, pk: int, membership_id: int | None = None) -> HttpResponse:
        host, membership = self.target(request, pk, membership_id)
        if not consume_once_token(request):
            return self.already_submitted(request, detail_url(host, "memberships"))
        form = MembershipForm(request.POST, request.FILES, instance=membership, host=host)
        if form.is_valid():
            record = form.save(commit=False)
            certificate = form.cleaned_data.get("certificate")
            today = timezone.localdate()
            try:
                with transaction.atomic():
                    result = host_records.save_membership(record, by=_actor(request), today=today)
                    if certificate:
                        host_records.replace_membership_certificate(
                            record,
                            file=certificate,
                            valid_until=record.valid_until,
                            by=_actor(request),
                            today=today,
                        )
            except ValidationError as error:
                apply_errors(form, _certificate_error(error))
            else:
                messages.success(request, _("Saved."))
                sync_messages(request, result)
                return redirect(detail_url(host, "memberships"))
        return self.page(request, host, membership, form)


def _certificate_error(error: ValidationError) -> ValidationError:
    """添付の検査のエラー（キーなし）を証明書の欄に付ける。"""
    if hasattr(error, "error_dict"):
        return error
    return ValidationError({"certificate": error.messages})


class CertificateView(HostFormView):
    """加入証明の差し替え（古い証明書は残る。加入状況の有効期限も新しい証明書に合わせる）。"""

    def target(self, request: HttpRequest, pk: int, membership_id: int) -> Any:
        host = self.host(request, pk)
        return host, get_object_or_404(host.memberships, pk=membership_id)

    def page(
        self, request: HttpRequest, host: HostOrganization, membership: HostMembership, form: Any
    ) -> HttpResponse:
        return self.render(
            request,
            host=host,
            form=form,
            title=_("Replace the certificate: %(body)s")
            % {"body": membership.field.membership_body_name},
            lead=_(
                "The previous certificate is kept. When the expiry date is extended, the task "
                "of the previous expiry date is completed automatically."
            ),
            back=detail_url(host, "memberships"),
            submit=_("Replace"),
            form_id="certificate-form",
            multipart=True,
        )

    def get(self, request: HttpRequest, pk: int, membership_id: int) -> HttpResponse:
        host, membership = self.target(request, pk, membership_id)
        form = CertificateForm(initial={"valid_until": membership.valid_until})
        return self.page(request, host, membership, form)

    def post(self, request: HttpRequest, pk: int, membership_id: int) -> HttpResponse:
        host, membership = self.target(request, pk, membership_id)
        if not consume_once_token(request):
            return self.already_submitted(request, detail_url(host, "memberships"))
        form = CertificateForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                result = host_records.replace_membership_certificate(
                    membership,
                    file=form.cleaned_data["file"],
                    valid_until=form.cleaned_data["valid_until"],
                    by=_actor(request),
                    today=timezone.localdate(),
                )
            except ValidationError as error:
                form.add_error("file", error)
            else:
                messages.success(request, _("Replaced the certificate."))
                sync_messages(request, result)
                return redirect(detail_url(host, "memberships"))
        return self.page(request, host, membership, form)


# --- 協力確認書 --------------------------------------------------------------------


class CooperationEditView(HostFormView):
    def target(self, request: HttpRequest, pk: int, confirmation_id: int | None) -> Any:
        host = self.host(request, pk)
        confirmation = (
            get_object_or_404(host.cooperation_confirmations, pk=confirmation_id)
            if confirmation_id is not None
            else None
        )
        return host, confirmation

    def page(
        self,
        request: HttpRequest,
        host: HostOrganization,
        confirmation: HostCooperationConfirmation | None,
        form: Any,
    ) -> HttpResponse:
        return self.render(
            request,
            host=host,
            form=form,
            title=(
                _("Edit the cooperation confirmation")
                if confirmation
                else _("Register a cooperation confirmation")
            ),
            lead=_(
                "The host organization submits it for each site to the municipalities of the "
                "site and of the worker's residence (one if they are the same). It has no "
                "expiry date. Submit it again when the site's address, the residence or the "
                "contact person changes."
            ),
            back=detail_url(host, "cooperation"),
            submit=_("Save"),
            form_id="cooperation-form",
            multipart=confirmation is None,
        )

    def get(
        self, request: HttpRequest, pk: int, confirmation_id: int | None = None
    ) -> HttpResponse:
        host, confirmation = self.target(request, pk, confirmation_id)
        form = CooperationForm(instance=confirmation, host=host)
        return self.page(request, host, confirmation, form)

    def post(
        self, request: HttpRequest, pk: int, confirmation_id: int | None = None
    ) -> HttpResponse:
        host, confirmation = self.target(request, pk, confirmation_id)
        if not consume_once_token(request):
            return self.already_submitted(request, detail_url(host, "cooperation"))
        form = CooperationForm(request.POST, request.FILES, instance=confirmation, host=host)
        if form.is_valid():
            try:
                host_records.save_cooperation(
                    form.save(commit=False),
                    files=form.cleaned_data.get("uploads") or [],
                    by=_actor(request),
                )
            except ValidationError as error:
                apply_errors(form, error)
            else:
                messages.success(request, _("Saved."))
                return redirect(detail_url(host, "cooperation"))
        return self.page(request, host, confirmation, form)


# --- 支援委託契約 ------------------------------------------------------------------


class SupportContractEditView(HostFormView):
    def target(self, request: HttpRequest, pk: int, contract_id: int | None) -> Any:
        host = self.host(request, pk)
        contract = (
            get_object_or_404(host.support_contracts, pk=contract_id)
            if contract_id is not None
            else None
        )
        return host, contract

    def page(
        self,
        request: HttpRequest,
        host: HostOrganization,
        contract: SupportContract | None,
        form: Any,
    ) -> HttpResponse:
        return self.render(
            request,
            host=host,
            form=form,
            title=_("Edit the support contract") if contract else _("Register a support contract"),
            lead=_(
                "Entrustment of the whole support plan (full entrustment). Periods of the same "
                "host organization cannot overlap. When the next contract is registered, the "
                "renewal task of the previous contract is completed automatically."
            ),
            back=detail_url(host, "support-contracts"),
            submit=_("Save"),
            form_id="support-contract-form",
            multipart=contract is None,
        )

    def get(self, request: HttpRequest, pk: int, contract_id: int | None = None) -> HttpResponse:
        host, contract = self.target(request, pk, contract_id)
        form = SupportContractForm(instance=contract, host=host)
        return self.page(request, host, contract, form)

    def post(self, request: HttpRequest, pk: int, contract_id: int | None = None) -> HttpResponse:
        host, contract = self.target(request, pk, contract_id)
        if not consume_once_token(request):
            return self.already_submitted(request, detail_url(host, "support-contracts"))
        form = SupportContractForm(request.POST, request.FILES, instance=contract, host=host)
        if form.is_valid():
            amendment = (
                host_records.Amendment(changed_on=form.cleaned_data["amended_on"])
                if form.cleaned_data.get("is_amendment")
                else None
            )
            try:
                result = host_records.save_support_contract(
                    form.save(commit=False),
                    files=form.cleaned_data.get("uploads") or [],
                    by=_actor(request),
                    today=timezone.localdate(),
                    amendment=amendment,
                )
            except ValidationError as error:
                apply_errors(form, error)
            else:
                messages.success(request, _("Saved."))
                sync_messages(request, result.sync)
                raised_messages(request, result.raised)
                return redirect(detail_url(host, "support-contracts"))
        return self.page(request, host, contract, form)


class SupportTerminateView(HostFormView):
    """支援委託契約の終了の登録（第3-3-2号・第3-2号を外国人ごとに起票。ADR-0017）。"""

    def target(self, request: HttpRequest, pk: int, contract_id: int) -> Any:
        host = self.host(request, pk)
        contract = get_object_or_404(host.support_contracts, pk=contract_id)
        if contract.terminated_on is not None:
            raise PermissionDenied
        return host, contract

    def page(
        self, request: HttpRequest, host: HostOrganization, contract: Any, form: Any
    ) -> HttpResponse:
        return self.render(
            request,
            host=host,
            form=form,
            title=_("Register the termination of the support contract"),
            lead=_(
                "Notification tasks (form 3-3-2 and form 3-2) are created for each foreign worker "
                "with an effective employment contract on the termination date. When the "
                "support ends because the worker leaves the job, register the termination of "
                "the employment contract instead (column c of form 3-1-2)."
            ),
            back=detail_url(host, "support-contracts"),
            submit=_("Register the termination"),
            form_id="support-termination-form",
        )

    def get(self, request: HttpRequest, pk: int, contract_id: int) -> HttpResponse:
        host, contract = self.target(request, pk, contract_id)
        return self.page(request, host, contract, SupportTerminationForm())

    def post(self, request: HttpRequest, pk: int, contract_id: int) -> HttpResponse:
        host, contract = self.target(request, pk, contract_id)
        if not consume_once_token(request):
            return self.already_submitted(request, detail_url(host, "support-contracts"))
        form = SupportTerminationForm(request.POST)
        if form.is_valid():
            try:
                result = host_records.terminate_support_contract(
                    contract,
                    terminated_on=form.cleaned_data["terminated_on"],
                    category=form.cleaned_data["termination_category"],
                    reason=form.cleaned_data["termination_reason"],
                    note=form.cleaned_data.get("termination_note") or "",
                    by=_actor(request),
                    today=timezone.localdate(),
                )
            except ValidationError as error:
                apply_errors(form, error)
            else:
                messages.success(request, _("Registered the termination."))
                sync_messages(request, result.sync)
                raised_messages(request, result.raised)
                return redirect(detail_url(host, "support-contracts"))
        return self.page(request, host, contract, form)


class SupportReopenView(HostFormView):
    """支援委託契約の終了の取消（管理者だけ。理由は必須）。"""

    required_action = Action.CONTRACTS_REOPEN

    def target(self, request: HttpRequest, pk: int, contract_id: int) -> Any:
        host = self.host(request, pk)
        contract = get_object_or_404(host.support_contracts, pk=contract_id)
        if contract.terminated_on is None:
            raise PermissionDenied
        return host, contract

    def form(self, data: Any = None) -> ReasonForm:
        form = ReasonForm(
            data, help_text=_("Example: the termination was registered for the wrong contract.")
        )
        form.fields["reason"].label = _("reason for cancelling the termination")
        return form

    def page(self, request: HttpRequest, host: HostOrganization, form: Any) -> HttpResponse:
        return self.render(
            request,
            host=host,
            form=form,
            title=_("Cancel the termination of the support contract"),
            lead=_(
                "Only for correcting a mistake. Notification tasks not yet started are cancelled "
                "with this reason. Tasks in progress are kept and marked; submitted or closed "
                "tasks get a note."
            ),
            back=detail_url(host, "support-contracts"),
            submit=_("Cancel the termination"),
            form_id="support-reopen-form",
        )

    def get(self, request: HttpRequest, pk: int, contract_id: int) -> HttpResponse:
        host, _contract = self.target(request, pk, contract_id)
        return self.page(request, host, self.form())

    def post(self, request: HttpRequest, pk: int, contract_id: int) -> HttpResponse:
        host, contract = self.target(request, pk, contract_id)
        if not consume_once_token(request):
            return self.already_submitted(request, detail_url(host, "support-contracts"))
        form = self.form(request.POST)
        if form.is_valid():
            try:
                result = host_records.cancel_support_termination(
                    contract,
                    reason=form.cleaned_data["reason"],
                    by=_actor(request),
                    today=timezone.localdate(),
                )
            except ValidationError as error:
                apply_errors(form, error)
            else:
                messages.success(request, _("Cancelled the termination."))
                raised_messages(request, result.raised)
                return redirect(detail_url(host, "support-contracts"))
        return self.page(request, host, form)


# --- 添付 ------------------------------------------------------------------------

ATTACHMENT_TARGETS: dict[str, tuple[str, str, Promise]] = {
    # URL の区分 → (受入れ機関からの関連名, 詳細の区分, 画面の見出し)
    "cooperation": (
        "cooperation_confirmations",
        "cooperation",
        gettext_lazy("Add a copy of the cooperation confirmation"),
    ),
    "support-contracts": (
        "support_contracts",
        "support-contracts",
        gettext_lazy("Add the support entrustment contract"),
    ),
}


class AttachmentAddView(HostFormView):
    """協力確認書の控え・支援委託契約書を足す（加入証明は差し替えの画面）。"""

    def target(self, request: HttpRequest, pk: int, section: str, object_id: int) -> Any:
        host = self.host(request, pk)
        if section not in ATTACHMENT_TARGETS:
            raise Http404
        related, anchor, title = ATTACHMENT_TARGETS[section]
        return host, get_object_or_404(getattr(host, related), pk=object_id), anchor, title

    def page(
        self, request: HttpRequest, host: HostOrganization, anchor: str, title: Any, form: Any
    ) -> HttpResponse:
        return self.render(
            request,
            host=host,
            form=form,
            title=title,
            lead="",
            back=detail_url(host, anchor),
            submit=_("Add"),
            form_id="attachment-form",
            multipart=True,
        )

    def get(self, request: HttpRequest, pk: int, section: str, object_id: int) -> HttpResponse:
        host, _target, anchor, title = self.target(request, pk, section, object_id)
        return self.page(request, host, anchor, title, AttachmentForm())

    def post(self, request: HttpRequest, pk: int, section: str, object_id: int) -> HttpResponse:
        host, target, anchor, title = self.target(request, pk, section, object_id)
        if not consume_once_token(request):
            return self.already_submitted(request, detail_url(host, anchor))
        form = AttachmentForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                host_records.add_attachments(
                    target, form.cleaned_data["uploads"], by=_actor(request)
                )
            except ValidationError as error:
                apply_errors(form, error)
            else:
                messages.success(request, _("Added the attachments."))
                return redirect(detail_url(host, anchor))
        return self.page(request, host, anchor, title, form)


SECTION_OF_KIND = {
    "membership_certificate": "memberships",
    "cooperation_confirmation": "cooperation",
    "support_contract": "support-contracts",
}


class AttachmentVoidView(HostFormView):
    """添付の取消（理由は必須。ファイルは消さずに打ち消して残す）。"""

    template_name = "hosts/attachment_void.html"

    def target(self, request: HttpRequest, pk: int, attachment_id: int) -> Any:
        host = self.host(request, pk)
        attachment = get_object_or_404(
            attachments.active(host_records.attachments_of_host(host)), pk=attachment_id
        )
        return host, attachment

    def page(
        self, request: HttpRequest, host: HostOrganization, attachment: Attachment, form: Any
    ) -> HttpResponse:
        return self.render(
            request,
            host=host,
            attachment=attachment,
            form=form,
            back=detail_url(host, SECTION_OF_KIND.get(attachment.kind, "")),
        )

    def form(self, data: Any = None) -> ReasonForm:
        return ReasonForm(data, help_text=_("Example: a file of another host was attached."))

    def get(self, request: HttpRequest, pk: int, attachment_id: int) -> HttpResponse:
        host, attachment = self.target(request, pk, attachment_id)
        return self.page(request, host, attachment, self.form())

    def post(self, request: HttpRequest, pk: int, attachment_id: int) -> HttpResponse:
        host, attachment = self.target(request, pk, attachment_id)
        back = detail_url(host, SECTION_OF_KIND.get(attachment.kind, ""))
        if not consume_once_token(request):
            return self.already_submitted(request, back)
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
                return redirect(back)
        return self.page(request, host, attachment, form)


class SupportFeesView(HostFormView):
    """委託料の内訳（第5-10号 別紙）。合計は月額と一致させる。内訳だけの変更は届出を起票しない。"""

    template_name = "hosts/support_fees_form.html"

    def target(self, request: HttpRequest, pk: int, contract_id: int) -> Any:
        host = self.host(request, pk)
        return host, get_object_or_404(host.support_contracts, pk=contract_id)

    def page(
        self,
        request: HttpRequest,
        host: Any,
        contract: Any,
        formset: Any,
        errors: list[str] | None = None,
    ) -> HttpResponse:
        return self.render(
            request,
            host=host,
            contract=contract,
            formset=formset,
            fee_errors=errors or [],
            back=detail_url(host, "support-contracts"),
        )

    def get(self, request: HttpRequest, pk: int, contract_id: int) -> HttpResponse:
        host, contract = self.target(request, pk, contract_id)
        rows = [
            {"name": fee.name, "amount": fee.amount, "timing": fee.timing}
            for fee in contract.fees.all()
        ] or [{}]
        return self.page(request, host, contract, SupportFeeFormSet(prefix="fees", initial=rows))

    def post(self, request: HttpRequest, pk: int, contract_id: int) -> HttpResponse:
        host, contract = self.target(request, pk, contract_id)
        if not consume_once_token(request):
            return self.already_submitted(request, detail_url(host, "support-contracts"))
        formset = SupportFeeFormSet(request.POST, prefix="fees")
        if formset.is_valid():
            rows = [
                host_records.FeeInput(
                    name=form.cleaned_data["name"],
                    amount=form.cleaned_data["amount"],
                    timing=form.cleaned_data["timing"],
                )
                for form in formset.forms
                if form.cleaned_data and not form.cleaned_data.get("DELETE")
            ]
            try:
                host_records.save_fees(contract, rows, by=_actor(request))
            except ValidationError as error:
                return self.page(
                    request, host, contract, formset, error.message_dict.get("fees", [])
                )
            else:
                messages.success(request, _("Saved the breakdown of the fee."))
                return redirect(detail_url(host, "support-contracts"))
        return self.page(request, host, contract, formset)
