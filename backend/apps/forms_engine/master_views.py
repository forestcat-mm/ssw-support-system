"""様式マスタの画面（設定の「様式マスタ」。F13、T53、ADR-0042）。

版の一覧・詳細、版の停止と解除（masters.manage）。新しい版の登録はリポジトリのデータと
`make forms`（D8）。別の版で作り直すのは生成物の画面（views.GeneratedRegenerateView）。
"""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.core.models import Action, User
from apps.core.settings_views import SettingsView
from apps.core.web_forms import consume_once_token
from apps.forms_engine.forms import SuspendForm
from apps.forms_engine.models import FormOwner, FormTemplate
from services import audit, form_master


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)
    return request.user


class FormMasterView(SettingsView):
    required_action = Action.MASTERS_MANAGE
    tab = "forms"
    template_name = "forms_engine/master_list.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        return self.render(request, groups=form_master.groups())


class FormVersionView(SettingsView):
    required_action = Action.MASTERS_MANAGE
    tab = "forms"
    template_name = "forms_engine/master_detail.html"

    def template(self, pk: int) -> FormTemplate:
        return get_object_or_404(
            FormTemplate.objects.select_related("mapping"), pk=pk, owner=FormOwner.COMMON
        )

    def page(self, request: HttpRequest, template: FormTemplate, **extra: Any) -> HttpResponse:
        mapping = template.mapping
        definition = mapping.definition
        source = (template.manifest or {}).get("source", {})
        return self.render(
            request,
            template=template,
            mapping=mapping,
            field_count=len(definition.get("fields", [])),
            rule_count=len(mapping.rules or []),
            source=source,
            siblings=form_master.siblings(template),
            document_count=template.generated_documents.count(),
            suspend_form=extra.get("suspend_form") or SuspendForm(),
        )

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        template = self.template(pk)
        audit.record_view(template)
        return self.page(request, template)

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        template = self.template(pk)
        url = reverse("settings:form_version", args=[template.pk])
        if not consume_once_token(request):
            return self.already_submitted(request, url)
        if request.POST.get("action") == "resume":
            form_master.resume(template, by=_actor(request))
            messages.success(request, _("Resumed the version."))
            return redirect(url)
        form = SuspendForm(request.POST)
        if not form.is_valid():
            return self.page(request, template, suspend_form=form)
        try:
            form_master.suspend(template, reason=form.cleaned_data["reason"], by=_actor(request))
        except ValidationError as error:
            form.add_error(None, error)
            return self.page(request, template, suspend_form=form)
        messages.success(request, _("Suspended the version. The previous version is used."))
        return redirect(url)
