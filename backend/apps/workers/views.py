"""外国人の業務画面（第5章 F01）。"""

from dataclasses import dataclass
from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView

from apps.core.models import Action, User
from apps.core.permissions import ActionRequiredMixin
from apps.core.web_forms import consume_once_token, once_context
from apps.workers.forms import (
    RECORD_FORMS,
    DeactivateForm,
    HistoryRecordForm,
    VoidForm,
    WorkerForm,
    WorkerListForm,
    apply_service_errors,
)
from apps.workers.models import (
    ForeignWorker,
    PassportRecord,
    ResidenceRecord,
    WorkerAddress,
    WorkerContact,
)
from services import (
    audit,
    authorization,
    contracts,
    deadlines,
    worker_detail,
    worker_list,
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
        counts = result.level_counts
        context.update(
            form=form,
            rows=result.rows,
            level_counts=[
                (level, counts[level])
                for level in (
                    deadlines.Level.OVERDUE,
                    deadlines.Level.WITHIN_7,
                    deadlines.Level.WITHIN_14,
                )
                if counts[level]
            ],
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


DETAIL_TABS = [
    Tab("basic", _("Basic"), "workers:detail"),
    Tab("contracts", _("Contract"), "workers:contracts", Action.CONTRACTS_VIEW),
    Tab("filings", _("Filings"), "workers:filings", Action.FILINGS_VIEW),
    Tab("history", _("History"), "workers:history"),
]


class WorkerDetailView(ActionRequiredMixin, TemplateView):
    """外国人詳細（T13c）。タブごとに URL を分け、表示のたびに閲覧を監査ログに 1 件残す。"""

    required_action = Action.WORKERS_VIEW
    tab = "basic"
    template_name = "workers/detail_basic.html"
    worker: ForeignWorker

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
            can_manage=active and authorization.has_action(user, Action.WORKERS_MANAGE),
            can_deactivate=active and authorization.has_action(user, Action.WORKERS_DEACTIVATE),
        )
        shown = self.tab_context(context)
        audit.record_view(self.worker, context={"tab": self.tab, **shown})
        return context

    def tab_context(self, context: dict[str, Any]) -> dict[str, Any]:
        """タブの中身を context に足し、監査ログの context（表示した行の ID など）を返す。"""
        info = worker_detail.basic_info(self.worker)
        context["info"] = info
        return {"shown": info.shown_ids(), "decrypted": info.decrypted_fields()}


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
        context.update(
            contracts=[(c, contracts.status_of(c, today)) for c in all_contracts],
            view=view,
        )
        if view is None:
            return {"shown": {}}
        return {
            "shown": {"contract": view.contract.pk, "contract_version": view.version.pk},
        }


class WorkerFilingsView(WorkerDetailView):
    tab = "filings"
    template_name = "workers/detail_filings.html"

    def tab_context(self, context: dict[str, Any]) -> dict[str, Any]:
        rows = worker_detail.worker_filings(self.worker)
        context["filing_rows"] = rows
        return {"shown": {"filings": [row.filing.pk for row in rows]}}


class WorkerHistoryView(WorkerDetailView):
    tab = "history"
    template_name = "workers/detail_history.html"

    def tab_context(self, context: dict[str, Any]) -> dict[str, Any]:
        entries = worker_detail.history(self.worker)
        context["entries"] = entries
        shown: dict[str, list[int]] = {}
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

    def form(self, worker: ForeignWorker, kind: str, data: Any = None) -> HistoryRecordForm:
        if kind not in RECORD_FORMS:
            raise Http404
        model: Any = RECORD_MODELS[kind]
        instance = model(worker=worker)
        return RECORD_FORMS[kind](data, instance=instance, worker_status=worker.status)

    def get(self, request: HttpRequest, pk: int, kind: str) -> HttpResponse:
        worker = self.worker(request, pk)
        form = self.form(worker, kind)
        return self.render(request, worker=worker, form=form, title=RECORD_TITLES[kind])

    def post(self, request: HttpRequest, pk: int, kind: str) -> HttpResponse:
        worker = self.worker(request, pk)
        form = self.form(worker, kind, request.POST)
        detail = reverse("workers:detail", args=[pk])
        if not consume_once_token(request):
            return self.already_submitted(request, detail)
        if not form.is_valid():
            return self.render(request, worker=worker, form=form, title=RECORD_TITLES[kind])
        workers.add_record(worker, form.save(commit=False), by=_actor(request))
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
