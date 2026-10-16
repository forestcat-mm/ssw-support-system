"""期限ダッシュボードと届出タスクの処理（第5章 F06・F07、T14）。"""

from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import TemplateView

from apps.core.models import Action, User
from apps.core.permissions import ActionRequiredMixin
from apps.core.web_forms import consume_once_token, once_context
from apps.filings.forms import (
    ASSIGNEE_ALL,
    ASSIGNEE_MINE,
    STATUS_AWAITING,
    DashboardForm,
    DetailsForm,
    ManualFilingForm,
    RebaseForm,
    TransitionForm,
    own_staff_id,
)
from apps.filings.models import Filing, FilingKind, FilingStatus, Trigger
from services import audit, authorization, dashboard, filings, workers

# ダッシュボードの絞り込みの条件（処理の画面から同じ条件で戻るため）
DASHBOARD_QUERY_SESSION_KEY = "dashboard_query"


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)
    return request.user


def default_assignee(user: User) -> str:
    """支援担当者（担当者に紐づくログインユーザー）は自分の担当、管理者は全員。"""
    if authorization.has_action(user, Action.USERS_MANAGE) or own_staff_id(user) is None:
        return ASSIGNEE_ALL
    return ASSIGNEE_MINE


def dashboard_url(request: HttpRequest) -> str:
    query = request.session.get(DASHBOARD_QUERY_SESSION_KEY, "")
    return reverse("home") + (f"?{query}" if query else "")


class DashboardView(ActionRequiredMixin, TemplateView):
    """期限ダッシュボード（ログイン後のトップ）。届出タスクを期限の近い順に出す。"""

    template_name = "filings/dashboard.html"
    required_action = Action.FILINGS_VIEW

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        user = _actor(self.request)
        self.request.session[DASHBOARD_QUERY_SESSION_KEY] = self.request.GET.urlencode()
        form = DashboardForm(self.request.GET, default_assignee=default_assignee(user))
        query = form.to_query(user)
        result = dashboard.dashboard(query, timezone.localdate())
        base = self.request.GET.copy()
        base.pop("level", None)
        base.pop("status", None)
        base["assignee"] = form.selected_assignee()
        context.update(
            form=form,
            selected_assignee=form.selected_assignee(),
            rows=result.rows,
            level_counts=[
                (level, result.level_counts[level], query.level is level)
                for level in dashboard.COUNTED_LEVELS
            ],
            awaiting_receipt=result.awaiting_receipt,
            awaiting_selected=(form.cleaned_data.get("status") == STATUS_AWAITING),
            chip_query=base.urlencode(),
            can_manage=authorization.has_action(user, Action.FILINGS_MANAGE),
        )
        return context


class FilingDetailView(ActionRequiredMixin, View):
    """届出タスクの処理。状態の変更・担当者とメモ・起算日の訂正を別々のフォームで保存する。"""

    required_action = Action.FILINGS_VIEW
    template_name = "filings/detail.html"
    FORMS = ("transition", "details", "rebase")

    def filing(self, pk: int) -> Filing:
        queryset = dashboard.base_queryset().select_related("worker", "assignee", "content_type")
        return get_object_or_404(queryset, pk=pk)

    def render(self, request: HttpRequest, filing: Filing, **forms: Any) -> HttpResponse:
        can_manage = authorization.has_action(request.user, Action.FILINGS_MANAGE)
        is_open = filing.status in dashboard.OPEN_STATUSES
        (row,) = dashboard.filing_rows([filing], timezone.localdate())
        context = {
            "filing": filing,
            "row": row,
            "definition": filings.definition(filing.kind),
            "category_label": filings.CATEGORY_LABELS[filings.definition(filing.kind).category],
            "can_manage": can_manage,
            "transition_form": forms.get("transition")
            or (
                TransitionForm(filing=filing)
                if filings.TRANSITIONS[FilingStatus(filing.status)]
                else None
            ),
            "details_form": forms.get("details") or DetailsForm(instance=filing),
            "rebase_form": forms.get("rebase") or (RebaseForm() if is_open else None),
            "findings": filing.trigger_details or {},
            "back_url": dashboard_url(request),
            **once_context(request),
        }
        return TemplateResponse(request, self.template_name, context)

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        filing = self.filing(pk)
        audit.record_view(filing)
        return self.render(request, filing)

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        if not authorization.has_action(request.user, Action.FILINGS_MANAGE):
            raise PermissionDenied
        filing = self.filing(pk)
        if not consume_once_token(request):
            messages.info(request, _("This form has already been submitted."))
            return redirect("filings:detail", pk)
        which = request.POST.get("form", "")
        actor = _actor(request)
        if which == "transition":
            form = TransitionForm(request.POST, filing=filing)
            if not form.is_valid():
                return self.render(request, filing, transition=form)
            to = FilingStatus(form.cleaned_data["to"])
            filings.transition(
                filing,
                to,
                by=actor,
                submitted_on=form.cleaned_data.get("submitted_on"),
                receipt_number=form.cleaned_data.get("receipt_number") or "",
                reason=form.cleaned_data.get("reason") or "",
            )
            messages.success(request, _("Changed the status to %(status)s.") % {"status": to.label})
            return redirect(dashboard_url(request))
        if which == "details":
            details = DetailsForm(request.POST, instance=Filing.objects.get(pk=filing.pk))
            if not details.is_valid():
                return self.render(request, filing, details=details)
            filings.update_details(
                filing,
                by=actor,
                assignee=details.cleaned_data.get("assignee"),
                note=details.cleaned_data.get("note") or "",
                submitted_on=details.cleaned_data.get("submitted_on"),
                receipt_number=details.cleaned_data.get("receipt_number"),
            )
            messages.success(request, _("Saved."))
            return redirect("filings:detail", pk)
        if which == "rebase":
            rebase = RebaseForm(request.POST)
            if rebase.is_valid():
                try:
                    filings.rebase(
                        filing,
                        rebase.cleaned_data["basis_on"],
                        by=actor,
                        reason=rebase.cleaned_data["reason"],
                    )
                except ValidationError as error:
                    rebase.add_error(None, error)
                else:
                    messages.success(request, _("Recalculated the due date."))
                    return redirect("filings:detail", pk)
            return self.render(request, filing, rebase=rebase)
        return redirect("filings:detail", pk)


class FilingCreateView(ActionRequiredMixin, View):
    """手動の起票（外国人詳細の「届出を起票」から、対象の外国人を入れた状態で開ける）。"""

    required_action = Action.FILINGS_MANAGE
    template_name = "filings/form.html"

    def render(self, request: HttpRequest, form: ManualFilingForm) -> HttpResponse:
        context = {"form": form, "back_url": dashboard_url(request), **once_context(request)}
        return TemplateResponse(request, self.template_name, context)

    def get(self, request: HttpRequest) -> HttpResponse:
        initial: dict[str, Any] = {"basis_on": timezone.localdate()}
        worker_id = request.GET.get("worker", "")
        if worker_id.isdigit():
            worker = workers.active_workers().filter(pk=int(worker_id)).first()
            if worker is not None:
                initial["worker"] = worker
                initial["assignee"] = worker.assigned_staff
        return self.render(request, ManualFilingForm(initial=initial))

    def post(self, request: HttpRequest) -> HttpResponse:
        if not consume_once_token(request):
            messages.info(request, _("This form has already been submitted."))
            return redirect(dashboard_url(request))
        form = ManualFilingForm(request.POST)
        if not form.is_valid():
            return self.render(request, form)
        filing, created = filings.create(
            FilingKind(form.cleaned_data["kind"]),
            target=form.cleaned_data["target"],
            basis_on=form.cleaned_data["basis_on"],
            trigger=Trigger.MANUAL,
            by=_actor(request),
            assignee=form.cleaned_data.get("assignee"),
            note=form.cleaned_data.get("note") or "",
        )
        if created:
            messages.success(request, _("Created the filing task."))
        else:
            messages.info(request, _("The same task already exists."))
        return redirect("filings:detail", filing.pk)
