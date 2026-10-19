"""受入れ機関の一覧・詳細（第5章 F02、T13e）。登録・編集の画面は edit_views（T28a）。"""

from typing import Any

from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.generic import TemplateView

from apps.core.models import Action, User
from apps.core.permissions import ActionRequiredMixin
from apps.hosts.models import HostOrganization
from services import (
    audit,
    authorization,
    career_plans,
    condition_templates,
    deadlines,
    host_detail,
    host_documents,
)


class HostListView(ActionRequiredMixin, TemplateView):
    """受入れ機関一覧。表示した一覧は閲覧として監査ログに 1 件残す（代表者の氏名を含むため）。"""

    template_name = "hosts/list.html"
    required_action = Action.HOSTS_VIEW

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        user = self.request.user
        assert isinstance(user, User)
        rows = host_detail.host_rows(user, timezone.localdate())
        audit.record_list_view(
            "hosts:list", conditions={}, object_ids=[row.host.pk for row in rows]
        )
        context["rows"] = rows
        context["can_manage"] = authorization.has_action(user, Action.HOSTS_MANAGE)
        return context


class HostDetailView(ActionRequiredMixin, TemplateView):
    """受入れ機関の詳細。表示のたびに閲覧を監査ログに 1 件残す。"""

    template_name = "hosts/detail.html"
    required_action = Action.HOSTS_VIEW

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        user = self.request.user
        assert isinstance(user, User)
        queryset = authorization.scope(user, HostOrganization.objects.all())
        host = get_object_or_404(queryset, pk=kwargs["pk"])
        today = timezone.localdate()
        with_workers = authorization.has_action(user, Action.WORKERS_VIEW)
        detail = host_detail.host_detail(host, user, today, with_workers=with_workers)
        audit.record_view(
            host,
            context={
                "shown": {
                    "sites": [site.pk for site in detail.sites],
                    "workers": [row.worker.pk for row in detail.workers],
                }
            },
        )
        context.update(
            host=host,
            detail=detail,
            today=today,
            with_workers=with_workers,
            can_manage=authorization.has_action(user, Action.HOSTS_MANAGE),
            can_reopen=authorization.has_action(user, Action.CONTRACTS_REOPEN),
        )
        context["career_plans"] = career_plans.plans_of(host)  # キャリアアッププラン（T36）
        # 受入れ機関・事業所の書類の保管庫（T42 の追加）
        context["host_document_rows"] = [
            {
                "document": document,
                "files": host_documents.files_of(document),
                "valid": host_documents.valid_on(document, today),
                "ending": host_documents.ending_soon(document, today),
                "old": document.kind in host_documents.ISSUED_KINDS
                and document.issued_on is not None
                and document.issued_on < deadlines.add_months(today, -host_documents.ISSUED_MONTHS),
            }
            for document in host_documents.live(host)
        ]
        # 雇用条件のひな形（T41。報酬などの契約の値を含むので、契約を見る権限も要る）
        can_see_templates = authorization.has_action(user, Action.CONTRACTS_VIEW)
        context["can_see_templates"] = can_see_templates
        if can_see_templates:
            context["condition_templates"] = condition_templates.of_host(host)
        # 相談記録書（第5-4号。複数の外国人の個人情報を含むので、外国人の閲覧の権限も要る。T35）
        can_generate = with_workers and authorization.has_action(user, Action.FORMS_GENERATE)
        context["can_generate_consultations"] = can_generate
        if can_generate:
            from apps.forms_engine.views import consultation_documents

            context["consultation_documents"] = consultation_documents(host)
        return context
