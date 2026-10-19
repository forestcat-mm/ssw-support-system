"""キャリアアッププランの業務画面（受入れ機関の詳細から開く。T36、ADR-0025）。

作成・新しい版・使っていない版の直しは hosts.manage。表示は hosts.view。生成・交付に使った版は
変えない（修正 1 回目 J1、ADR-0029）。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views import View

from apps.core.models import Action, User
from apps.core.permissions import ActionRequiredMixin
from apps.core.web_forms import consume_once_token
from apps.hosts.career_plan_forms import CareerPlanForm, LevelFormSet, levels_of
from apps.hosts.edit_views import HostFormView
from apps.hosts.models import CareerPlan, CareerPlanVersion, HostOrganization
from services import audit, authorization, career_plans


def plan_url(plan: CareerPlan) -> str:
    return reverse("hosts:career_plan", args=[plan.host_organization_id, plan.pk])


class CareerPlanEditView(HostFormView):
    """プランを作る（plan_id なし）・新しい版を足す（前の版を写して開く）・使っていない版を直す
    （number あり。修正 1 回目 J1）。"""

    template_name = "hosts/career_plan_form.html"

    def target(
        self, request: HttpRequest, pk: int, plan_id: int | None, number: int | None = None
    ) -> tuple[HostOrganization, CareerPlan | None, CareerPlanVersion | None]:
        host = self.host(request, pk)
        plan = get_object_or_404(host.career_plans, pk=plan_id) if plan_id is not None else None
        editing = None
        if plan is not None and number is not None:
            editing = get_object_or_404(plan.versions, version=number)
            if not career_plans.is_editable(editing):
                raise PermissionDenied
        return host, plan, editing

    def page(
        self,
        request: HttpRequest,
        host: HostOrganization,
        plan: CareerPlan | None,
        form: CareerPlanForm,
        levels: Any,
        editing: CareerPlanVersion | None = None,
    ) -> HttpResponse:
        back = plan_url(plan) if plan else reverse("hosts:detail", args=[host.pk]) + "#career-plans"
        if editing is not None:
            back += f"?version={editing.version}"
        return self.render(
            request, host=host, plan=plan, form=form, levels=levels, back=back, editing=editing
        )

    def get(
        self, request: HttpRequest, pk: int, plan_id: int | None = None, number: int | None = None
    ) -> HttpResponse:
        host, plan, editing = self.target(request, pk, plan_id, number)
        initial: dict[str, Any] = {}
        rows: list[dict[str, Any]] = [{}]
        source = editing or (career_plans.latest_version(plan) if plan else None)
        if source is not None:
            data = career_plans.initial_from(source)
            initial = {"career_route": data.career_route, "transfer_scope": data.transfer_scope}
            rows = [asdict(level) for level in data.levels]
        if editing is not None:
            initial |= {
                "applies_from": editing.applies_from.isoformat(),
                "change_reason": editing.change_reason,
            }
        form = CareerPlanForm(
            initial=initial, host=host, new_plan=plan is None, first_version=_first(editing)
        )
        levels = LevelFormSet(prefix="levels", initial=rows)
        return self.page(request, host, plan, form, levels, editing)

    def post(
        self, request: HttpRequest, pk: int, plan_id: int | None = None, number: int | None = None
    ) -> HttpResponse:
        host, plan, editing = self.target(request, pk, plan_id, number)
        if not consume_once_token(request):
            return self.already_submitted(request, reverse("hosts:detail", args=[host.pk]))
        form = CareerPlanForm(
            request.POST, host=host, new_plan=plan is None, first_version=_first(editing)
        )
        levels = LevelFormSet(request.POST, prefix="levels")
        if form.is_valid() and levels.is_valid():
            data = career_plans.VersionInput(
                applies_from=form.cleaned_data["applies_from"],
                career_route=form.cleaned_data["career_route"],
                transfer_scope=form.cleaned_data.get("transfer_scope") or "",
                change_reason=form.cleaned_data.get("change_reason") or "",
                levels=levels_of(levels),
            )
            actor = request.user
            assert isinstance(actor, User)
            try:
                if plan is None:
                    version = career_plans.create_plan(
                        host,
                        form.cleaned_data["field"],
                        form.cleaned_data.get("site"),
                        data,
                        by=actor,
                    )
                elif editing is not None:
                    version = career_plans.edit_version(editing, data, by=actor)
                else:
                    version = career_plans.add_version(plan, data, by=actor)
            except ValidationError as error:
                if hasattr(error, "error_dict"):
                    for key, messages_ in error.message_dict.items():
                        form.add_error(key if key in form.fields else None, messages_)
                else:
                    form.add_error(None, error)
            else:
                messages.success(
                    request,
                    _("Saved the career-up plan (version %(n)d).") % {"n": version.version},
                )
                return redirect(plan_url(version.plan) + f"?version={version.version}")
        return self.page(request, host, plan, form, levels, editing)


def _first(editing: CareerPlanVersion | None) -> bool:
    """直している版が第 1 版か（第 1 版は変更理由が要らない）。"""
    return editing is not None and editing.version == 1


class CareerPlanDetailView(ActionRequiredMixin, View):
    """プランの版とレベル（?version= で版を選ぶ）。表示は受入れ機関の閲覧として記録する。"""

    required_action = Action.HOSTS_VIEW

    def get(self, request: HttpRequest, pk: int, plan_id: int) -> HttpResponse:
        host = get_object_or_404(
            authorization.scope(request.user, HostOrganization.objects.all()), pk=pk
        )
        plan = get_object_or_404(host.career_plans.select_related("field", "site"), pk=plan_id)
        versions = list(plan.versions.order_by("-version"))
        requested = request.GET.get("version", "")
        version = next((v for v in versions if str(v.version) == requested), versions[0])
        audit.record_view(host, context={"career_plan": plan.pk, "version": version.pk})
        return TemplateResponse(
            request,
            "hosts/career_plan_detail.html",
            {
                "host": host,
                "plan": plan,
                "versions": versions,
                "version": version,
                "levels": list(version.levels.order_by("order", "pk")),
                "editable": career_plans.is_editable(version),
                "can_manage": authorization.has_action(request.user, Action.HOSTS_MANAGE),
            },
        )
