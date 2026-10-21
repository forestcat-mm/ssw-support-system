"""受入れ機関の一覧・詳細（第5章 F02、T13e）。登録・編集の画面は edit_views（T28a）。"""

from typing import Any

from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
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
    host_declarations,
    host_detail,
    host_documents,
    host_overview,
    immigration_codes,
)

# 受入れ機関の詳細のセクション（U7）: 並び・id・見出し。
# 権限で出さないものは番号を詰める
HOST_SECTIONS = (
    ("host-basic", _("Basic information")),
    ("sites", _("Sites and operation categories")),
    ("memberships", _("Memberships")),
    ("cooperation", _("Cooperation confirmations")),
    ("support-contracts", _("Support contracts")),
    ("condition-templates", _("Employment condition templates")),
    ("host-documents", _("Documents of the host organization")),
    ("host-application", _("Items for the bulk application")),
    ("career-plans", _("Career-up plans")),
    ("periodic-report", _("Periodic notification (form 3-6)")),
    ("consultation-records", _("Consultation records (form 5-4)")),
    ("host-workers", _("Foreign workers of this host organization")),
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
                    "officers": list(host.officers.values_list("pk", flat=True)),
                    "applicant_contacts": list(
                        host.applicant_contacts.values_list("pk", flat=True)
                    ),
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
        # 一括申請の項目（T46b。ADR-0038）
        declaration = host_declarations.latest(host)
        context["application"] = {
            "industries": [
                (
                    immigration_codes.label(immigration_codes.INDUSTRY, code),
                    other,
                )
                for code, other in (
                    (host.industry_1, host.industry_1_other),
                    (host.industry_2, host.industry_2_other),
                    (host.industry_3, host.industry_3_other),
                )
                if code
            ],
            "declaration": declaration,
            "unusual": host_declarations.unusual(declaration) if declaration else [],
            "missing": host_declarations.missing(declaration) if declaration else [],
            "financial_years": list(host.financial_years.all()),
            "officers": list(host.officers.all()),
            "contacts": list(host.applicant_contacts.all()),
        }
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
        # 上の帯・今やること・番号つきのセクションと目次（U7）
        context["head"] = host_overview.header(detail, today)
        context["can_see_todos"] = authorization.has_action(user, Action.FILINGS_VIEW)
        if context["can_see_todos"]:
            context["todos"] = host_overview.todos(host, today)
        hidden = set()
        if not can_see_templates:
            hidden.add("condition-templates")
        if not can_generate:
            hidden.update({"periodic-report", "consultation-records"})
        if not with_workers:
            hidden.add("host-workers")
        shown = [(key, label) for key, label in HOST_SECTIONS if key not in hidden]
        context["section_no"] = {
            key.replace("-", "_"): number for number, (key, _label) in enumerate(shown, 1)
        }
        context["toc"] = [
            {"id": key, "label": f"{number} {label}"}
            for number, (key, label) in enumerate(shown, 1)
        ]
        return context
