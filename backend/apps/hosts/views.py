"""受入れ機関の一覧・詳細（第5章 F02、T13e）。登録・編集の画面は edit_views（T28a）。"""

from typing import Any

from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.generic import TemplateView

from apps.core.models import Action, User
from apps.core.permissions import ActionRequiredMixin
from apps.hosts.models import HostOrganization
from services import audit, authorization, host_detail


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
        )
        return context
