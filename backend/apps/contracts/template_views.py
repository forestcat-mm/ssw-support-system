"""雇用条件のひな形の画面（受入れ機関の詳細から。T41、ADR-0034）。"""

from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.contracts.forms import (
    DETAIL_LABELS,
    AllowanceFormSet,
    ConditionTemplateForm,
    ConditionTemplateMetaForm,
    DeductionFormSet,
    ShiftFormSet,
    rows_of,
)
from apps.contracts.models import EmploymentConditionTemplate
from apps.core.models import Action, User
from apps.core.web_forms import consume_once_token
from apps.hosts.edit_views import HostFormView
from apps.hosts.models import HostOrganization
from services import audit, authorization, condition_templates, contracts


class TemplateEditor:
    """ひな形の版の欄・明細の行・名前と印の組。"""

    def __init__(
        self, data: Any = None, *, host: HostOrganization, template: EmploymentConditionTemplate
    ) -> None:
        initial, rows = condition_templates.edit_initial(template) if template.pk else ({}, {})
        self.form = ConditionTemplateForm(data, initial=initial, host=host)
        self.shifts = ShiftFormSet(data, prefix="shifts", initial=rows.get("shifts"))
        self.allowances = AllowanceFormSet(
            data, prefix="allowances", initial=rows.get("allowances")
        )
        self.deductions = DeductionFormSet(
            data, prefix="deductions", initial=rows.get("deductions")
        )
        names = list(self.form.fields)
        meta_initial = {
            "name": template.name,
            "is_active": template.is_active if template.pk else True,
            "marked": list(template.marked),
        }
        self.meta = ConditionTemplateMetaForm(data, initial=meta_initial, names=names)
        self.names = names

    @property
    def marked(self) -> set[str]:
        if self.meta.is_bound:
            return set(self.meta.data.getlist("marked"))
        return set(self.meta.initial.get("marked") or [])

    def is_valid(self) -> bool:
        results = [
            self.meta.is_valid(),
            self.form.is_valid(),
            self.shifts.is_valid(),
            self.allowances.is_valid(),
            self.deductions.is_valid(),
        ]
        return all(results)

    def details(self) -> contracts.Details:
        return contracts.Details(
            shifts=rows_of(self.shifts),
            allowances=rows_of(self.allowances),
            deductions=rows_of(self.deductions),
        )


class ConditionTemplateEditView(HostFormView):
    """ひな形の追加（template_id なし）と編集。"""

    template_name = "contracts/condition_template_form.html"

    def target(
        self, request: HttpRequest, pk: int, template_id: int | None
    ) -> tuple[HostOrganization, EmploymentConditionTemplate]:
        host = self.host(request, pk)
        if not authorization.has_action(request.user, Action.CONTRACTS_VIEW):
            raise PermissionDenied  # 報酬などの契約の値を含む
        if template_id is None:
            return host, EmploymentConditionTemplate(host_organization=host)
        return host, get_object_or_404(host.condition_templates, pk=template_id)

    def back(self, host: HostOrganization) -> str:
        return reverse("hosts:detail", args=[host.pk]) + "#condition-templates"

    def page(
        self,
        request: HttpRequest,
        host: HostOrganization,
        template: EmploymentConditionTemplate,
        editor: TemplateEditor,
    ) -> HttpResponse:
        return self.render(
            request,
            host=host,
            template=template,
            editor=editor,
            marked=editor.marked,
            detail_labels=DETAIL_LABELS,
            back=self.back(host),
        )

    def get(self, request: HttpRequest, pk: int, template_id: int | None = None) -> HttpResponse:
        host, template = self.target(request, pk, template_id)
        if template.pk:
            audit.record_view(template)
        return self.page(request, host, template, TemplateEditor(host=host, template=template))

    def post(self, request: HttpRequest, pk: int, template_id: int | None = None) -> HttpResponse:
        host, template = self.target(request, pk, template_id)
        if not consume_once_token(request):
            return self.already_submitted(request, self.back(host))
        editor = TemplateEditor(request.POST, host=host, template=template)
        if editor.is_valid():
            assert isinstance(request.user, User)
            template.name = editor.meta.cleaned_data["name"].strip()
            template.is_active = editor.meta.cleaned_data["is_active"]
            try:
                condition_templates.save_template(
                    template,
                    values=condition_templates.values_from(editor.form.cleaned_data, editor.names),
                    details=condition_templates.details_from(editor.details()),
                    marked=editor.meta.cleaned_data["marked"],
                    allowed=editor.names,
                    by=request.user,
                )
            except ValidationError as error:
                for key, errors in error.error_dict.items():
                    form = editor.meta if key in ("name", "marked") else editor.form
                    form.add_error(key if key in form.fields else None, errors)
            else:
                messages.success(request, _("Saved the template."))
                return redirect(self.back(host))
        return self.page(request, host, template, editor)
