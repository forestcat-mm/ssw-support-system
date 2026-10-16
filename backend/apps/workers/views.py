"""外国人の業務画面（第5章 F01）。"""

from dataclasses import dataclass
from typing import Any

from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponseBase
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView

from apps.core.models import Action, User
from apps.core.permissions import ActionRequiredMixin
from apps.workers.forms import WorkerListForm
from apps.workers.models import ForeignWorker
from services import audit, authorization, contracts, deadlines, worker_detail, worker_list

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
        form = WorkerListForm(self.request.GET)
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
