"""外国人の業務画面（第5章 F01）。"""

from typing import Any

from django.utils import timezone
from django.views.generic import TemplateView

from apps.core.models import Action, User
from apps.core.permissions import ActionRequiredMixin
from apps.workers.forms import WorkerListForm
from services import audit, deadlines, worker_list


class WorkerListView(ActionRequiredMixin, TemplateView):
    """外国人一覧（T13b）。表示した一覧は閲覧として監査ログに 1 件残す。"""

    template_name = "workers/list.html"
    required_action = Action.WORKERS_VIEW

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        user = self.request.user
        assert isinstance(user, User)
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
