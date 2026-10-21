"""外国人の業務画面（第5章 F01）。"""

import io
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView

from apps.core import list_tabs
from apps.core.models import Action, AuditAction, Staff, User
from apps.core.permissions import ActionRequiredMixin
from apps.core.web_forms import (
    PeriodVoidForm,
    StaffPeriodForm,
    consume_once_token,
    once_context,
)
from apps.workers.forms import (
    RECORD_FORMS,
    CertificateFormMixin,
    DeactivateForm,
    HistoryRecordForm,
    ReactivateForm,
    VoidForm,
    WorkerForm,
    WorkerListForm,
    apply_service_errors,
)
from apps.workers.models import (
    ForeignWorker,
    InternshipCompletion,
    PassportRecord,
    ResidenceRecord,
    StaffAssignment,
    WorkerAddress,
    WorkerContact,
    WorkerQualification,
    WorkerRelative,
    WorkerStatus,
    WorkHistory,
)
from services import (
    audit,
    authorization,
    book_exports,
    books,
    contracts,
    form_convert,
    staff_periods,
    wage_explanations,
    worker_detail,
    worker_documents,
    worker_list,
    worker_overview,
    worker_photos,
    workers,
)

# 一覧の検索・絞り込みの条件（「一覧へ戻る」で同じ条件に戻すため）
LIST_QUERY_SESSION_KEY = "workers_list_query"


class WorkerListView(ActionRequiredMixin, TemplateView):
    """外国人一覧（T13b）。表示した一覧は閲覧として監査ログに 1 件残す。"""

    template_name = "workers/list.html"
    required_action = Action.WORKERS_VIEW

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        user = self.request.user
        assert isinstance(user, User)
        self.request.session[LIST_QUERY_SESSION_KEY] = self.request.GET.urlencode()
        form = WorkerListForm(
            self.request.GET,
            can_see_deactivated=authorization.has_action(user, Action.WORKERS_DEACTIVATE),
        )
        query = form.to_query(user)
        result = worker_list.list_workers(user, query, timezone.localdate())
        audit.record_list_view(
            "workers:list",
            conditions=form.cleaned_data,
            object_ids=[row.worker.pk for row in result.rows],
        )
        base = list_tabs.query_without(self.request.GET, "level")
        context.update(
            form=form,
            rows=result.rows,
            level_tabs=list_tabs.level_tabs(base, result.total, result.level_counts, query.level),
            # 一覧の中に検索の欄は出さない（上の帯の検索に入れたまま）。絞り込みで送るときも残す
            keep=[("level", query.level.value if query.level else ""), ("q", query.text)],
            filtered=any(value for key, value in self.request.GET.items() if key != "order"),
            can_manage=authorization.has_action(user, Action.WORKERS_MANAGE),
        )
        return context


@dataclass(frozen=True)
class Tab:
    """詳細画面のタブ。段階2以降で「支援記録」「帳票」をここに 1 行ずつ足す。"""

    key: str
    label: str | Promise
    url_name: str
    action: Action | None = None  # workers.view のほかに要る操作（無ければタブごと出さない）


# タブの並びと名前（仕様 5.2: 概要・契約・支援・届出・帳票・帳簿・履歴）。
# キー（監査ログの context.tab）は変えない
DETAIL_TABS = [
    Tab("basic", _("Overview"), "workers:detail"),
    Tab("contracts", _("Contract"), "workers:contracts", Action.CONTRACTS_VIEW),
    Tab("support", _("Support"), "workers:support", Action.SUPPORT_VIEW),
    Tab("filings", _("Filings"), "workers:filings", Action.FILINGS_VIEW),
    Tab("documents", _("Forms"), "workers:documents", Action.FORMS_GENERATE),
    # 登録支援機関の帳簿（③④。T57、ADR-0044）
    Tab("books", _("Books"), "workers:books", Action.SUPPORT_VIEW),
    Tab("history", _("History"), "workers:history"),
]


class WorkerDetailView(ActionRequiredMixin, TemplateView):
    """外国人詳細（T13c）。タブごとに URL を分け、表示のたびに閲覧を監査ログに 1 件残す。"""

    required_action = Action.WORKERS_VIEW
    tab = "basic"
    template_name = "workers/detail_basic.html"
    worker: ForeignWorker
    info: worker_detail.BasicInfo
    head: worker_overview.Header

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        current = next(tab for tab in DETAIL_TABS if tab.key == self.tab)
        if (
            request.user.is_authenticated
            and current.action is not None
            and not authorization.has_action(request.user, current.action)
        ):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        user = self.request.user
        queryset = authorization.scope(user, ForeignWorker.objects.all()).select_related(
            "nationality", "support_language", "assigned_staff"
        )
        self.worker = get_object_or_404(queryset, pk=kwargs["pk"])
        name, name_lang, romaji = worker_list.display_name(self.worker)
        active = workers.is_active(self.worker)
        # 上の帯（仕様 5.2）: どのタブでも出す。在留情報は最新の行（概要のタブでも使う）
        self.info = worker_detail.basic_info(self.worker)
        today = timezone.localdate()
        self.head = worker_overview.header(self.worker, self.info, today)
        list_query = self.request.session.get(LIST_QUERY_SESSION_KEY, "")
        context.update(
            worker=self.worker,
            display_name=name,
            display_name_lang=name_lang,
            romaji=romaji,
            native_lang=worker_list.NATIVE_NAME_LANGUAGES.get(self.worker.nationality.code, ""),
            tabs=[
                {
                    "label": tab.label,
                    "url": reverse(tab.url_name, args=[self.worker.pk]),
                    "active": tab.key == self.tab,
                }
                for tab in DETAIL_TABS
                if tab.action is None or authorization.has_action(user, tab.action)
            ],
            back_url=reverse("workers:list") + (f"?{list_query}" if list_query else ""),
            head=self.head,
            initial=worker_overview.initial_of(self.worker),
            photo=worker_photos.latest(self.worker),  # 申請用の写真（U4）
            toc=[],
            can_manage=active and authorization.has_action(user, Action.WORKERS_MANAGE),
            can_deactivate=active and authorization.has_action(user, Action.WORKERS_DEACTIVATE),
            can_reactivate=not active and authorization.has_action(user, Action.WORKERS_DEACTIVATE),
        )
        shown = self.tab_context(context)
        audit.record_view(self.worker, context={"tab": self.tab, **shown})
        return context

    def tab_context(self, context: dict[str, Any]) -> dict[str, Any]:
        """タブの中身を context に足し、監査ログの context（表示した行の ID など）を返す。"""
        info = self.info
        documents = worker_documents.rows_for(self.request.user, self.worker)
        user = self.request.user
        allowed = worker_overview.Allowed(
            contracts=authorization.has_action(user, Action.CONTRACTS_VIEW),
            support=authorization.has_action(user, Action.SUPPORT_VIEW),
            filings=authorization.has_action(user, Action.FILINGS_VIEW),
            forms=authorization.has_action(user, Action.FORMS_GENERATE),
        )
        found = worker_overview.overview(
            self.worker, self.head, info, timezone.localdate(), allowed
        )
        context.update(
            info=info,
            application_sections=worker_detail.application_sections(info),
            health_documents=documents,
            retention_until=books.retention_until(self.worker),  # 帳簿の保存義務の満了日（T44）
            overview=found,
            toc=overview_toc(found),
        )
        shown = info.shown_ids() | {"health_documents": [row.attachment.pk for row in documents]}
        if found.contract is not None:  # 5 雇用契約の要約（U3）
            shown["contract_version"] = found.contract.version.pk
        return {"shown": shown, "decrypted": info.decrypted_fields()}


def overview_toc(found: worker_overview.Overview) -> list[dict[str, Any]]:
    """概要のタブの目次（仕様 5.2。注意のあるセクションに点）。"""
    items = [
        ("todo", _("To do")),
        ("personal", _("1 Personal information")),
        ("residence", _("2 Residence")),
        ("passport", _("3 Passport")),
        ("address", _("4 Address and contact")),
        ("contract", _("5 Employment contract")),
        ("support", _("6 Support")),
        ("application-items", _("7 Items for the bulk application")),
        ("health-documents", _("8 Health check documents")),
    ]
    hidden = set()
    if found.allowed is not None and not found.allowed.contracts:
        hidden.add("contract")
    if found.allowed is not None and not found.allowed.support:
        hidden.add("support")
    return [
        {"id": key, "label": label, "warn": key in found.warnings}
        for key, label in items
        if key not in hidden
    ]


# 契約のタブの「3 契約の書類」に出す様式（生成の画面と同じ ID）
CONTRACT_FORM_IDS = [
    "1-6",
    "1-5",
    "1-4",
    "career-plan",
    "experience-certificate",
    "3-1",
    "13-1",
    "13-2",
    "5-10",
    "1-25",
]


class WorkerContractsView(WorkerDetailView):
    tab = "contracts"
    template_name = "workers/detail_contracts.html"

    def tab_context(self, context: dict[str, Any]) -> dict[str, Any]:
        today = timezone.localdate()
        all_contracts = worker_detail.contracts_of(self.worker)
        requested = self.request.GET.get("contract", "")
        selected = next((c for c in all_contracts if str(c.pk) == requested), None)
        selected = selected or worker_detail.default_contract(all_contracts, today)
        version = self.request.GET.get("version", "")
        view = (
            worker_detail.contract_view(
                selected, int(version) if version.isdigit() else None, today
            )
            if selected is not None
            else None
        )
        user = self.request.user
        active = workers.is_active(self.worker)
        context.update(
            contracts=[(c, contracts.status_of(c, today)) for c in all_contracts],
            view=view,
            can_manage_contracts=active and authorization.has_action(user, Action.CONTRACTS_MANAGE),
            can_reopen=active and authorization.has_action(user, Action.CONTRACTS_REOPEN),
            conversion_warning=contracts.conversion_warning(view.version) if view else "",
            conversion_terms_missing=(
                contracts.conversion_terms_missing(view.version) if view else False
            ),
            can_generate_forms=active and authorization.has_action(user, Action.FORMS_GENERATE),
            wage_explanation=(
                wage_explanations.explanation_of(view.version) if view is not None else None
            ),
        )
        if view is None:
            return {"shown": {}}
        # 見本 05 の形（U5）: 1 雇用契約・2 版の履歴（届出）・3 契約の書類（状態）・4 支援委託契約
        documents = worker_overview.contract_documents(
            self.worker, view.contract, view.version, CONTRACT_FORM_IDS
        )
        filings_by_version = worker_overview.version_filings(self.worker, view.contract)
        summary = worker_overview.contract_summary(view.contract, today)
        context.update(
            contract_documents=documents,
            version_rows=[(item, filings_by_version.get(item.version)) for item in view.versions],
            support_contract=summary.support_contract if summary else None,
            toc=[
                {"id": "contract-main", "label": _("1 Employment contract"), "warn": False},
                {"id": "versions", "label": _("2 Version history"), "warn": False},
                {
                    "id": "contract-documents",
                    "label": _("3 Contract documents"),
                    "warn": any(doc.tone == "danger" for doc in documents.values()),
                },
                {"id": "support-contract", "label": _("4 Support contract"), "warn": False},
            ],
        )
        return {
            "shown": {"contract": view.contract.pk, "contract_version": view.version.pk},
        }


class WorkerFilingsView(WorkerDetailView):
    tab = "filings"
    template_name = "workers/detail_filings.html"

    def tab_context(self, context: dict[str, Any]) -> dict[str, Any]:
        rows = worker_detail.worker_filings(self.worker)
        context["filing_rows"] = rows
        context["toc"] = [
            {"id": "worker-filings", "label": _("1 Filing tasks"), "warn": False},
        ]
        context["can_create_filing"] = workers.is_active(self.worker) and authorization.has_action(
            self.request.user, Action.FILINGS_MANAGE
        )
        return {"shown": {"filings": [row.filing.pk for row in rows]}}


class WorkerHistoryView(WorkerDetailView):
    tab = "history"
    template_name = "workers/detail_history.html"

    def tab_context(self, context: dict[str, Any]) -> dict[str, Any]:
        entries = worker_detail.history(self.worker)
        context["entries"] = entries
        context["toc"] = [
            {"id": "staff-periods", "label": _("1 Assigned staff"), "warn": False},
            {"id": "photos", "label": _("2 Application photos"), "warn": False},
            {"id": "ledger-history", "label": _("3 History of the ledger"), "warn": False},
        ]
        photos = worker_photos.history(self.worker)
        # 「使っている写真」は取り消していない最新の写真（並びは新しい順）
        context["photos"] = photos
        periods = staff_periods.worker_periods(self.worker)
        context["staff_periods"] = periods
        shown: dict[str, list[int]] = {
            "staff_assignment": [period.pk for period in periods],
            "worker_photo": [photo.pk for photo in photos],
        }
        for entry in entries:
            shown.setdefault(entry.kind, []).append(entry.record.pk)
        return {"shown": shown}


# --- 登録・編集（T13d）。権限は workers.manage。監査ログは保存のシグナルで残る ---


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)
    return request.user


RECORD_TITLES = {
    "residence": _("Add residence record"),
    "passport": _("Add passport"),
    "address": _("Add address"),
    "contact": _("Add contact"),
    "relative": _("Add a relative or housemate in Japan"),
    "qualification": _("Add a skill or Japanese language qualification"),
    "internship": _("Add completed Technical Intern Training (ii)"),
    "work": _("Add work history"),
}
REGISTER_SECTIONS = [
    ("residence", _("Residence (optional)")),
    ("passport", _("Passport (optional)")),
    ("address", _("Address (optional)")),
    ("contact", _("Contact (optional)")),
]
RECORD_MODELS: dict[str, type[Any]] = {
    "residence": ResidenceRecord,
    "passport": PassportRecord,
    "address": WorkerAddress,
    "contact": WorkerContact,
    "relative": WorkerRelative,
    "qualification": WorkerQualification,
    "internship": InternshipCompletion,
    "work": WorkHistory,
}


class WorkerFormView(ActionRequiredMixin, View):
    """登録・編集の画面の共通部分（二重送信の防止と、表示）。"""

    required_action = Action.WORKERS_MANAGE
    template_name = ""

    def render(self, request: HttpRequest, **context: Any) -> HttpResponse:
        return TemplateResponse(request, self.template_name, {**context, **once_context(request)})

    def already_submitted(self, request: HttpRequest, redirect_to: str) -> HttpResponse:
        messages.info(request, _("This form has already been submitted."))
        return redirect(redirect_to)

    def worker(self, request: HttpRequest, pk: int) -> ForeignWorker:
        """対象の外国人。無効化済みの人は編集・追加・取消・無効化の対象にしない（403）。"""
        queryset = authorization.scope(request.user, ForeignWorker.objects.all())
        worker = get_object_or_404(queryset, pk=pk)
        if not workers.is_active(worker):
            raise PermissionDenied
        return worker


class WorkerCreateView(WorkerFormView):
    """新規登録。基本情報と、任意で最初の在留情報・旅券・住所・連絡先を 1 画面で入れる。"""

    template_name = "workers/register.html"

    def forms(
        self, data: Any = None
    ) -> tuple[WorkerForm, list[tuple[str, Any, HistoryRecordForm]]]:
        status = data.get("status") if data is not None else None
        sections = [
            (
                key,
                label,
                RECORD_FORMS[key](
                    data,
                    prefix=key,
                    empty_permitted=True,
                    use_required_attribute=False,
                    worker_status=status,
                ),
            )
            for key, label in REGISTER_SECTIONS
        ]
        return WorkerForm(data), sections

    def get(self, request: HttpRequest) -> HttpResponse:
        form, sections = self.forms()
        return self.render(request, form=form, sections=sections)

    def post(self, request: HttpRequest) -> HttpResponse:
        if not consume_once_token(request):
            return self.already_submitted(request, reverse("workers:list"))
        form, sections = self.forms(request.POST)
        valid = form.is_valid()
        valid = all([section.is_valid() for _key, _label, section in sections]) and valid
        if not valid:
            return self.render(request, form=form, sections=sections)
        records = [
            section.save(commit=False)
            for _key, _label, section in sections
            if section.has_changed()
        ]
        worker = workers.register_worker(form.save(commit=False), records, by=_actor(request))
        messages.success(request, _("Registered the foreign worker."))
        return redirect("workers:detail", worker.pk)


class WorkerUpdateView(WorkerFormView):
    """基本情報の編集。開いた後にほかの人が更新していたら上書きしない。"""

    template_name = "workers/form.html"

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        worker = self.worker(request, pk)
        return self.render(
            request, worker=worker, form=WorkerForm(instance=worker), title=_("Edit")
        )

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        worker = self.worker(request, pk)
        detail = reverse("workers:detail", args=[pk])
        if not consume_once_token(request):
            return self.already_submitted(request, detail)
        form = WorkerForm(request.POST, instance=worker)
        if form.is_valid():
            try:
                workers.update_worker(
                    form.save(commit=False),
                    by=_actor(request),
                    loaded_updated_at=form.cleaned_data.get("loaded_updated_at"),
                )
            except ValidationError as error:
                apply_service_errors(form, error)
            else:
                messages.success(request, _("Saved."))
                return redirect(detail)
        return self.render(request, worker=worker, form=form, title=_("Edit"))


class RecordCreateView(WorkerFormView):
    """履歴行（在留情報・旅券・住所・連絡先）の追加。1 操作 1 画面。"""

    template_name = "workers/form.html"

    def form(
        self, worker: ForeignWorker, kind: str, data: Any = None, files: Any = None
    ) -> HistoryRecordForm:
        if kind not in RECORD_FORMS:
            raise Http404
        model: Any = RECORD_MODELS[kind]
        instance = model(worker=worker)
        return RECORD_FORMS[kind](data, files, instance=instance, worker_status=worker.status)

    def get(self, request: HttpRequest, pk: int, kind: str) -> HttpResponse:
        worker = self.worker(request, pk)
        form = self.form(worker, kind)
        return self.render(
            request,
            worker=worker,
            form=form,
            title=RECORD_TITLES[kind],
            pre_entry_note=kind == "residence" and worker.status == WorkerStatus.PRE_ENTRY,
            multipart=isinstance(form, CertificateFormMixin),
        )

    def post(self, request: HttpRequest, pk: int, kind: str) -> HttpResponse:
        worker = self.worker(request, pk)
        form = self.form(worker, kind, request.POST, request.FILES)
        detail = reverse("workers:detail", args=[pk])
        if not consume_once_token(request):
            return self.already_submitted(request, detail)
        if not form.is_valid():
            return self.render(
                request,
                worker=worker,
                form=form,
                title=RECORD_TITLES[kind],
                pre_entry_note=kind == "residence" and worker.status == WorkerStatus.PRE_ENTRY,
                multipart=isinstance(form, CertificateFormMixin),
            )
        files = form.files_to_attach() if isinstance(form, CertificateFormMixin) else []
        workers.add_record(worker, form.save(commit=False), by=_actor(request), files=files)
        messages.success(request, _("Added: %(what)s") % {"what": RECORD_TITLES[kind]})
        return redirect(f"{detail}#{kind}")


class RecordVoidView(WorkerFormView):
    """履歴行の取消（理由は必須）。この画面が確認画面で、「取り消す」を押すと取り消す。"""

    template_name = "workers/void.html"

    def entry(self, worker: ForeignWorker, kind: str, record_id: int) -> worker_detail.HistoryEntry:
        if kind not in RECORD_MODELS:
            raise Http404
        for entry in worker_detail.history(worker):
            if entry.kind == kind and entry.record.pk == record_id and not entry.voided:
                return entry
        raise Http404

    def get(self, request: HttpRequest, pk: int, kind: str, record_id: int) -> HttpResponse:
        worker = self.worker(request, pk)
        entry = self.entry(worker, kind, record_id)
        return self.render(request, worker=worker, entry=entry, form=VoidForm())

    def post(self, request: HttpRequest, pk: int, kind: str, record_id: int) -> HttpResponse:
        worker = self.worker(request, pk)
        history_url = reverse("workers:history", args=[pk])
        if not consume_once_token(request):
            return self.already_submitted(request, history_url)
        entry = self.entry(worker, kind, record_id)
        form = VoidForm(request.POST)
        if form.is_valid():
            try:
                workers.void_record(
                    entry.record, by=_actor(request), reason=form.cleaned_data["reason"]
                )
            except ValidationError as error:
                form.add_error("reason", error)
            else:
                messages.success(request, _("Voided the record."))
                return redirect(history_url)
        return self.render(request, worker=worker, entry=entry, form=form)


class StaffChangeView(WorkerFormView):
    """担当者を変える（新しい担当者・開始日・理由。T45-D4）。履歴の行を足す。"""

    template_name = "workers/staff_change.html"

    def form(self, data: Any = None) -> StaffPeriodForm:
        return StaffPeriodForm(
            data,
            queryset=Staff.objects.filter(is_active=True).order_by("name_kana", "name"),
            label=_("new assigned staff"),
            empty_label=_("not assigned"),
        )

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        worker = self.worker(request, pk)
        return self.render(request, worker=worker, form=self.form())

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        worker = self.worker(request, pk)
        history_url = reverse("workers:history", args=[pk]) + "#staff-periods"
        if not consume_once_token(request):
            return self.already_submitted(request, history_url)
        form = self.form(request.POST)
        if form.is_valid():
            try:
                staff_periods.change_worker_staff(
                    worker,
                    form.cleaned_data["staff"],
                    starts_on=form.cleaned_data["starts_on"],
                    reason=form.cleaned_data["reason"],
                    by=_actor(request),
                    today=timezone.localdate(),
                )
            except ValidationError as error:
                apply_service_errors(form, error)
            else:
                messages.success(request, _("Changed the assigned staff."))
                return redirect(history_url)
        return self.render(request, worker=worker, form=form)


class StaffVoidView(WorkerFormView):
    """担当者の履歴の行の取消（理由は必須）。"""

    template_name = "workers/staff_void.html"

    def period(self, worker: ForeignWorker, period_id: int) -> StaffAssignment:
        return get_object_or_404(
            StaffAssignment.objects.select_related("staff"),
            pk=period_id,
            worker=worker,
            voided_at__isnull=True,
        )

    def get(self, request: HttpRequest, pk: int, period_id: int) -> HttpResponse:
        worker = self.worker(request, pk)
        period = self.period(worker, period_id)
        return self.render(request, worker=worker, period=period, form=PeriodVoidForm())

    def post(self, request: HttpRequest, pk: int, period_id: int) -> HttpResponse:
        worker = self.worker(request, pk)
        history_url = reverse("workers:history", args=[pk]) + "#staff-periods"
        if not consume_once_token(request):
            return self.already_submitted(request, history_url)
        period = self.period(worker, period_id)
        form = PeriodVoidForm(request.POST)
        if form.is_valid():
            staff_periods.void_worker_period(
                period, by=_actor(request), reason=form.cleaned_data["reason"]
            )
            messages.success(request, _("Voided the record."))
            return redirect(history_url)
        return self.render(request, worker=worker, period=period, form=form)


class WorkerDeactivateView(WorkerFormView):
    """外国人の無効化（管理者のみ・理由必須）。この画面が確認画面で、影響を先に見せる。"""

    required_action = Action.WORKERS_DEACTIVATE
    template_name = "workers/deactivate.html"

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        worker = self.worker(request, pk)
        effects = workers.deactivation_effects(worker)
        return self.render(request, worker=worker, effects=effects, form=DeactivateForm())

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        worker = self.worker(request, pk)
        detail = reverse("workers:detail", args=[pk])
        if not consume_once_token(request):
            return self.already_submitted(request, detail)
        form = DeactivateForm(request.POST)
        if form.is_valid():
            try:
                workers.deactivate(worker, by=_actor(request), reason=form.cleaned_data["reason"])
            except ValidationError as error:
                form.add_error("reason", error)
            else:
                messages.success(request, _("Deactivated the foreign worker."))
                return redirect(detail)
        effects = workers.deactivation_effects(worker)
        return self.render(request, worker=worker, effects=effects, form=form)


class WorkerReactivateView(ActionRequiredMixin, View):
    """誤って無効化した外国人を戻す（管理者のみ・理由必須。T15b で業務画面にも置く）。"""

    required_action = Action.WORKERS_DEACTIVATE
    template_name = "workers/reactivate.html"

    def worker(self, request: HttpRequest, pk: int) -> ForeignWorker:
        queryset = authorization.scope(request.user, ForeignWorker.objects.all())
        worker = get_object_or_404(queryset, pk=pk)
        if workers.is_active(worker):
            raise Http404
        return worker

    def render(self, request: HttpRequest, worker: ForeignWorker, form: Any) -> HttpResponse:
        context = {"worker": worker, "form": form, **once_context(request)}
        return TemplateResponse(request, self.template_name, context)

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        return self.render(request, self.worker(request, pk), ReactivateForm())

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        worker = self.worker(request, pk)
        detail = reverse("workers:detail", args=[pk])
        if not consume_once_token(request):
            messages.info(request, _("This form has already been submitted."))
            return redirect(detail)
        form = ReactivateForm(request.POST)
        if not form.is_valid():
            return self.render(request, worker, form)
        workers.reactivate(worker, by=_actor(request), reason=form.cleaned_data["reason"])
        messages.success(request, _("Cancelled the deactivation."))
        return redirect(detail)


class WorkerBooksView(WorkerDetailView):
    """帳簿（③ 支援対象者・④ 支援の実施。T57、ADR-0044）。在留カード番号を出す（監査ログ）。"""

    tab = "books"
    template_name = "workers/detail_books.html"

    def tab_context(self, context: dict[str, Any]) -> dict[str, Any]:
        book = book_exports.worker_book(self.worker)
        context["book"] = book
        context["toc"] = [
            {"id": "book-recipient", "label": _("1 Support recipient"), "warn": False},
            {"id": "book-support", "label": _("2 Support carried out"), "warn": False},
            {"id": "book-documents", "label": _("3 Related forms"), "warn": False},
        ]
        return {
            "shown": {"books": [self.worker.pk]},
            "decrypted": ["card_number"] if book.card_number else [],
        }


class WorkerBookPdfView(ActionRequiredMixin, View):
    """外国人ごとの帳簿の PDF（自作の xlsx を PDF に。保存しない。監査ログに EXPORT。T57-D2）。"""

    required_action = Action.SUPPORT_VIEW

    def get(self, request: HttpRequest, pk: int) -> FileResponse:
        if not authorization.has_action(request.user, Action.WORKERS_VIEW):
            raise PermissionDenied
        worker = get_object_or_404(
            authorization.scope(request.user, ForeignWorker.objects.all()), pk=pk
        )
        book = book_exports.worker_book(worker)
        with tempfile.TemporaryDirectory(prefix="book-") as folder:
            source = Path(folder) / "book.xlsx"
            source.write_bytes(book_exports.worker_book_xlsx(book))
            pdf = form_convert.convert(source).read_bytes()
        audit.record(
            AuditAction.EXPORT,
            target=worker,
            actor=request.user if isinstance(request.user, User) else None,
            context={"books": "worker", "records": len(book.records)},
        )
        name = f"帳簿_{worker.name_romaji}_{timezone.localdate():%Y%m%d}.pdf"
        return FileResponse(io.BytesIO(pdf), as_attachment=True, filename=name)
