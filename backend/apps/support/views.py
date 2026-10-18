"""支援計画の画面（第5章 F04、T17）。外国人詳細の「支援」タブと、計画の作成・新版の追加。

標準の支援計画（設定画面）は apps.core.settings_views に置く。
"""

from typing import Any

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.contracts.models import EmploymentContract
from apps.core.models import Action, User
from apps.core.web_forms import consume_once_token
from apps.forms_engine.forms import PlanGenerateForm
from apps.hosts.models import HostOrganization
from apps.support.display import plan_view
from apps.support.forms import PlanEditor
from apps.support.models import SupportPlan, SupportPlanVersion
from apps.workers.models import ForeignWorker
from apps.workers.views import WorkerDetailView, WorkerFormView
from services import (
    authorization,
    form_checks,
    form_data_1_17,
    form_generation,
    form_templates,
    support_plans,
    worker_detail,
    workers,
)


def current_host(worker: ForeignWorker) -> HostOrganization | None:
    """今の雇用契約（有効 → 予定 → 最後に終了）の受入れ機関。"""
    contract = worker_detail.default_contract(
        worker_detail.contracts_of(worker), timezone.localdate()
    )
    return contract.host_organization if contract else None


def contract_hosts(worker: ForeignWorker) -> list[HostOrganization]:
    """この外国人と雇用契約のある受入れ機関（計画を作れる相手）。"""
    ids = EmploymentContract.objects.filter(worker=worker).values_list(
        "host_organization_id", flat=True
    )
    return list(HostOrganization.objects.filter(pk__in=ids).order_by("name"))


class SupportPlanTabView(WorkerDetailView):
    """外国人詳細の「支援」タブの計画（今の受入れ機関の計画・版の一覧・差分）。"""

    tab = "support"
    template_name = "support/worker_plan.html"

    def tab_context(self, context: dict[str, Any]) -> dict[str, Any]:
        worker = self.worker
        today = timezone.localdate()
        plans = list(
            SupportPlan.objects.filter(worker=worker)
            .select_related("host_organization")
            .order_by("-created_at")
        )
        host = current_host(worker)
        requested = self.request.GET.get("plan", "")
        plan = next((p for p in plans if str(p.pk) == requested), None)
        plan = plan or next((p for p in plans if p.host_organization == host), None)
        plan = plan or (plans[0] if plans else None)
        user = self.request.user
        can_manage = workers.is_active(worker) and authorization.has_action(
            user, Action.SUPPORT_MANAGE
        )
        context.update(
            plans=plans,
            plan=plan,
            current_host=host,
            can_manage_support=can_manage,
            can_create_plan=can_manage
            and host is not None
            and not any(p.host_organization == host for p in plans),
            support_subtab="plan",
        )
        if plan is None:
            return {"shown": {}}
        versions = list(plan.versions.select_related("created_by").order_by("-version"))
        number = self.request.GET.get("version", "")
        version = next((v for v in versions if str(v.version) == number), None)
        version = version or support_plans.current_version(plan, today) or versions[0]
        previous = next((v for v in versions if v.version < version.version), None)
        content = support_plans.content_of(version)
        context.update(
            can_generate_forms=workers.is_active(worker)
            and authorization.has_action(user, Action.FORMS_GENERATE),
            versions=versions,
            version=version,
            previous=previous,
            view=plan_view(content, support_plans.warnings(content)),
            diff=support_plans.diff_rows(previous, version) if previous else [],
        )
        return {"shown": {"support_plan": plan.pk, "support_plan_version": version.pk}}


class PlanEditView(WorkerFormView):
    """計画の作成・新版の追加の共通部分（1 画面で全区分をまとめて保存する）。"""

    required_action = Action.SUPPORT_MANAGE
    template_name = "support/plan_edit.html"

    def tab_url(self, worker: ForeignWorker, plan: SupportPlan | None = None) -> str:
        url = reverse("workers:support", args=[worker.pk])
        return f"{url}?plan={plan.pk}" if plan else url

    def page(self, request: HttpRequest, editor: PlanEditor, **context: Any) -> HttpResponse:
        return self.render(
            request, editor=editor, sections=editor.sections(editor.warnings()), **context
        )


class PlanCreateView(PlanEditView):
    """計画の作成。標準の計画を複製し、担当者・支援言語を補った状態で開く。"""

    def context(
        self, worker: ForeignWorker, hosts: list[HostOrganization], selected: Any
    ) -> dict[str, Any]:
        return {
            "worker": worker,
            "hosts": hosts,
            "selected_host": selected,
            "title": _("Create a support plan"),
            "back_url": self.tab_url(worker),
        }

    def initial(self, worker: ForeignWorker) -> Any:
        content = support_plans.content_for_worker(worker)
        content.created_on = timezone.localdate()
        return content

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        worker = self.worker(request, pk)
        hosts = contract_hosts(worker)
        if not hosts:
            raise Http404
        editor = PlanEditor(initial=self.initial(worker), initial_applies_from=timezone.localdate())
        return self.page(request, editor, **self.context(worker, hosts, current_host(worker)))

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        worker = self.worker(request, pk)
        hosts = contract_hosts(worker)
        host = next((h for h in hosts if str(h.pk) == request.POST.get("host")), None)
        if not consume_once_token(request):
            return self.already_submitted(request, self.tab_url(worker))
        editor = PlanEditor(
            request.POST, initial=self.initial(worker), initial_applies_from=timezone.localdate()
        )
        if editor.is_valid():
            assert isinstance(request.user, User)
            if host is None:
                editor.version_form.add_error(None, _("Select the host organization."))
            else:
                try:
                    plan = support_plans.create_plan(
                        worker,
                        host,
                        applies_from=editor.version_form.cleaned_data["applies_from"],
                        by=request.user,
                        content=editor.to_content(),
                    )
                except ValidationError as error:
                    editor.apply_errors(error)
                else:
                    messages.success(request, _("Created the support plan."))
                    return redirect(self.tab_url(worker, plan))
        return self.page(request, editor, **self.context(worker, hosts, host))


class VersionCreateView(PlanEditView):
    """新版の追加（最新の版の中身で開く。適用開始日と変更理由は必須）。"""

    def target(
        self, request: HttpRequest, pk: int, plan_id: int
    ) -> tuple[ForeignWorker, SupportPlan]:
        worker = self.worker(request, pk)
        return worker, get_object_or_404(SupportPlan, pk=plan_id, worker=worker)

    def latest_content(self, plan: SupportPlan) -> Any:
        version = support_plans.latest_version(plan)
        assert version is not None
        content = support_plans.content_of(version)
        content.created_on = timezone.localdate()
        return content

    def context(self, worker: ForeignWorker, plan: SupportPlan) -> dict[str, Any]:
        return {
            "worker": worker,
            "plan": plan,
            "title": _("Add a new version of the support plan"),
            "back_url": self.tab_url(worker, plan),
        }

    def get(self, request: HttpRequest, pk: int, plan_id: int) -> HttpResponse:
        worker, plan = self.target(request, pk, plan_id)
        editor = PlanEditor(
            initial=self.latest_content(plan), initial_applies_from=timezone.localdate()
        )
        return self.page(request, editor, **self.context(worker, plan))

    def post(self, request: HttpRequest, pk: int, plan_id: int) -> HttpResponse:
        worker, plan = self.target(request, pk, plan_id)
        if not consume_once_token(request):
            return self.already_submitted(request, self.tab_url(worker, plan))
        editor = PlanEditor(
            request.POST,
            initial=self.latest_content(plan),
            initial_applies_from=timezone.localdate(),
        )
        if editor.is_valid():
            assert isinstance(request.user, User)
            try:
                version = support_plans.add_version(
                    plan,
                    editor.to_content(),
                    applies_from=editor.version_form.cleaned_data["applies_from"],
                    change_reason=editor.version_form.cleaned_data["change_reason"],
                    by=request.user,
                )
            except ValidationError as error:
                editor.apply_errors(error)
            else:
                messages.success(
                    request, _("Added version %(number)s.") % {"number": version.version}
                )
                return redirect(f"{self.tab_url(worker, plan)}&version={version.version}")
        return self.page(request, editor, **self.context(worker, plan))


NOT_LOADED = gettext_lazy(
    "The form is not loaded. Ask an administrator to load the forms (make forms)."
)


class PlanGenerateView(WorkerFormView):
    """支援計画書（第1-17号）の生成（計画の版ごと。T31、ADR-0020）。

    基準日（申請予定日）と申告を入れて検査し、生成不可が無ければ生成する。警告があるときは
    「警告を確かめた」の印が要る。生成は依頼（ジョブ）で、結果は生成物の画面で見る。
    """

    required_action = Action.FORMS_GENERATE
    template_name = "support/plan_generate.html"
    form_id = "1-17"

    def target(
        self, request: HttpRequest, pk: int, plan_id: int, number: int
    ) -> tuple[ForeignWorker, SupportPlanVersion]:
        worker = self.worker(request, pk)
        plan = get_object_or_404(SupportPlan, pk=plan_id, worker=worker)
        return worker, get_object_or_404(SupportPlanVersion, plan=plan, version=number)

    def data(self, version: SupportPlanVersion, form: PlanGenerateForm) -> dict[str, Any]:
        basis_on = form.cleaned_data["basis_on"]
        propriety = bool(form.cleaned_data.get("propriety"))
        return form_data_1_17.build(version, basis_on) | form_data_1_17.declaration_values(
            propriety
        )

    def page(
        self,
        request: HttpRequest,
        worker: ForeignWorker,
        version: SupportPlanVersion,
        form: PlanGenerateForm,
        result: form_checks.CheckResult | None,
    ) -> HttpResponse:
        back = reverse("workers:support", args=[worker.pk])
        return self.render(
            request,
            worker=worker,
            version=version,
            form=form,
            result=result,
            back_url=f"{back}?plan={version.plan_id}&version={version.version}",
        )

    def check(
        self, version: SupportPlanVersion, form: PlanGenerateForm
    ) -> form_checks.CheckResult | None:
        """検査する（様式が読み込まれていなければ、フォームのエラーにして None）。"""
        try:
            return form_generation.check(
                self.form_id,
                target=version,
                data=self.data(version, form),
                basis_on=form.cleaned_data["basis_on"],
            )
        except form_templates.FormTemplateError:
            form.add_error(None, NOT_LOADED)
            return None

    def get(self, request: HttpRequest, pk: int, plan_id: int, number: int) -> HttpResponse:
        worker, version = self.target(request, pk, plan_id, number)
        form = PlanGenerateForm({"basis_on": timezone.localdate().isoformat()})
        result = self.check(version, form) if form.is_valid() else None
        if result is None:
            return self.page(request, worker, version, form, None)
        form = PlanGenerateForm(initial={"basis_on": timezone.localdate()})
        return self.page(request, worker, version, form, result)

    def post(self, request: HttpRequest, pk: int, plan_id: int, number: int) -> HttpResponse:
        worker, version = self.target(request, pk, plan_id, number)
        form = PlanGenerateForm(request.POST)
        if not form.is_valid():
            return self.page(request, worker, version, form, None)
        if request.POST.get("action") != "generate":
            return self.page(request, worker, version, form, self.check(version, form))
        if not consume_once_token(request):
            return self.already_submitted(request, reverse("workers:support", args=[worker.pk]))
        assert isinstance(request.user, User)
        try:
            document = form_generation.request(
                self.form_id,
                target=version,
                data=self.data(version, form),
                basis_on=form.cleaned_data["basis_on"],
                by=request.user,
                accept_warnings=bool(form.cleaned_data.get("accept_warnings")),
            )
        except form_generation.GenerationBlocked as blocked:
            return self.page(request, worker, version, form, blocked.result)
        except form_templates.FormTemplateError:
            form.add_error(None, NOT_LOADED)
            return self.page(request, worker, version, form, None)
        except form_generation.WarningsNotAccepted as pending:
            form.add_error("accept_warnings", _("Check the warnings before generating."))
            return self.page(request, worker, version, form, pending.result)
        messages.success(request, _("Requested the generation of the form."))
        return redirect("forms:document", pk=document.pk)
