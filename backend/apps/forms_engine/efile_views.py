"""一括届出の画面（電子届出の一括申込用ツール。T50・T52。ADR-0040）。操作は forms.generate。

作る（ツール・届出機関）→ 届出タスクを入れる → 行ごとに届出の内容を確かめる → 検査 → 生成 →
ダウンロード（記入済み・監査ログ）。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views import View

from apps.core.models import Action, User
from apps.core.permissions import ActionRequiredMixin
from apps.core.web_forms import consume_once_token, once_context
from apps.filings.models import Filing
from apps.forms_engine.efile_forms import AddFilingsForm, BulkFilingForm, RowForm
from apps.forms_engine.models import BulkFiling, BulkFilingFile, BulkFilingRow
from services import audit, bulk_filings, efile_data, form_templates, worker_list


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)
    return request.user


def detail_url(bulk: BulkFiling, anchor: str = "") -> str:
    return reverse("forms:efile", args=[bulk.pk]) + (f"#{anchor}" if anchor else "")


def filing_label(filing: Filing) -> str:
    name = worker_list.display_name(filing.worker)[0] if filing.worker else filing.target_repr
    return f"{name} · {filing.get_kind_display()} · {filing.basis_on:%Y-%m-%d}"


class EfileView(ActionRequiredMixin, View):
    required_action = Action.FORMS_GENERATE
    template_name = ""

    def render(self, request: HttpRequest, **context: Any) -> HttpResponse:
        return TemplateResponse(request, self.template_name, {**context, **once_context(request)})

    def bulk(self, request: HttpRequest, pk: int) -> BulkFiling:
        bulk = get_object_or_404(BulkFiling.objects.select_related("host_organization"), pk=pk)
        if not bulk_filings.can_view(request.user, bulk):
            raise PermissionDenied
        return bulk

    def already_submitted(self, request: HttpRequest, url: str) -> HttpResponse:
        messages.info(request, _("This form has already been submitted."))
        return redirect(url)


class EfileListView(EfileView):
    template_name = "forms_engine/efile_list.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        bulks = BulkFiling.objects.select_related("host_organization").prefetch_related(
            "rows", "files"
        )
        rows = [{"bulk": bulk, "label": bulk_filings.label_of(bulk.form_id)} for bulk in bulks]
        return self.render(request, rows=rows)


class EfileCreateView(EfileView):
    template_name = "forms_engine/efile_form.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        return self.render(request, form=BulkFilingForm())

    def post(self, request: HttpRequest) -> HttpResponse:
        if not consume_once_token(request):
            return self.already_submitted(request, reverse("forms:efile_list"))
        form = BulkFilingForm(request.POST)
        if not form.is_valid():
            return self.render(request, form=form)
        bulk = bulk_filings.create(
            form.cleaned_data["form_id"],
            form.cleaned_data.get("host"),
            by=_actor(request),
            title=form.cleaned_data.get("title", ""),
        )
        messages.success(request, _("Created the bulk notification. Add filing tasks."))
        return redirect(detail_url(bulk))


class EfileDetailView(EfileView):
    template_name = "forms_engine/efile_detail.html"

    def page(self, request: HttpRequest, bulk: BulkFiling, **extra: Any) -> HttpResponse:
        rows = bulk_filings.rows_of(bulk)
        try:
            problems = bulk_filings.check(bulk)
            not_loaded = False
        except form_templates.FormTemplateError:
            problems, not_loaded = [], True
        by_row: dict[int, list[bulk_filings.Problem]] = defaultdict(list)
        general = []
        for problem in problems:
            if problem.row is None:
                general.append(problem)
            else:
                by_row[problem.row.pk].append(problem)
        blocking = sum(1 for problem in problems if problem.blocking)
        lines = [
            {
                "row": row,
                "name": worker_list.display_name(row.worker)[0],
                "errors": [p for p in by_row.get(row.pk, []) if p.blocking],
                "warnings": [p for p in by_row.get(row.pk, []) if not p.blocking],
                "estimated": bool((row.inputs or {}).get(efile_data.ESTIMATED)),
            }
            for row in rows
        ]
        files = [
            (record, bulk_filings.can_download(request.user, record))
            for record in bulk.files.select_related("template")
        ]
        choices = (
            [(task.pk, filing_label(task)) for task in bulk_filings.candidates(bulk)]
            if bulk.submitted_on is None
            else []
        )
        audit.record_view(
            bulk, context={"shown": {"workers": sorted({row.worker_id for row in rows})}}
        )
        return self.render(
            request,
            bulk=bulk,
            label=bulk_filings.label_of(bulk.form_id),
            lines=lines,
            general=general,
            blocking=blocking,
            not_loaded=not_loaded,
            files=files,
            add_form=extra.get("add_form") or AddFilingsForm(choices=choices),
            **extra,
        )

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        return self.page(request, self.bulk(request, pk))

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """生成（検査を通ったら）。"""
        bulk = self.bulk(request, pk)
        if not consume_once_token(request):
            return self.already_submitted(request, detail_url(bulk))
        try:
            created = bulk_filings.generate(bulk, by=_actor(request))
        except bulk_filings.GenerationBlocked as error:
            if error.problems:
                messages.error(
                    request,
                    _("Not generated: fix the %(count)s problem(s) shown for each row.")
                    % {"count": len(error.problems)},
                )
            else:
                messages.error(request, _("Add filing tasks first."))
            return redirect(detail_url(bulk, "rows"))
        messages.success(request, _("Generated %(count)s file(s).") % {"count": len(created)})
        return redirect(detail_url(bulk, "files"))


class EfileAddFilingsView(EfileView):
    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        bulk = self.bulk(request, pk)
        if not consume_once_token(request):
            return self.already_submitted(request, detail_url(bulk))
        allowed = {task.pk: task for task in bulk_filings.candidates(bulk)}
        added = 0
        for value in request.POST.getlist("filings"):
            task = allowed.get(int(value)) if value.isdigit() else None
            if task is None:
                continue
            added += len(bulk_filings.add_filing(bulk, task, by=_actor(request)))
        messages.success(request, _("Added %(count)s row(s).") % {"count": added})
        return redirect(detail_url(bulk, "rows"))


class EfileRowView(EfileView):
    """1 行の届出の内容（届出タスクからの既定値を直す）。"""

    template_name = "forms_engine/efile_row.html"

    def row(self, request: HttpRequest, pk: int, row_id: int) -> BulkFilingRow:
        bulk = self.bulk(request, pk)
        return get_object_or_404(
            BulkFilingRow.objects.select_related("bulk", "filing", "worker"),
            pk=row_id,
            bulk=bulk,
        )

    def form(self, row: BulkFilingRow, data: Any = None) -> RowForm:
        template = bulk_filings.template_of(row.bulk)
        return RowForm(data, row=row, definition=template.mapping.definition)

    def page(self, request: HttpRequest, row: BulkFilingRow, form: RowForm) -> HttpResponse:
        audit.record_view(row.worker, context={"bulk_filing": row.bulk_id})
        return self.render(
            request,
            bulk=row.bulk,
            label=bulk_filings.label_of(row.bulk.form_id),
            row=row,
            name=worker_list.display_name(row.worker)[0],
            form=form,
        )

    def get(self, request: HttpRequest, pk: int, row_id: int) -> HttpResponse:
        row = self.row(request, pk, row_id)
        return self.page(request, row, self.form(row))

    def post(self, request: HttpRequest, pk: int, row_id: int) -> HttpResponse:
        row = self.row(request, pk, row_id)
        if not consume_once_token(request):
            return self.already_submitted(request, detail_url(row.bulk, "rows"))
        if row.bulk.submitted_on is not None:
            messages.error(
                request, _("The submission of this bulk notification has been recorded.")
            )
            return redirect(detail_url(row.bulk, "rows"))
        form = self.form(row, request.POST)
        if form.is_valid():
            bulk_filings.save_inputs(row, form.inputs(), by=_actor(request))
            messages.success(request, _("Saved."))
            return redirect(detail_url(row.bulk, "rows"))
        return self.page(request, row, form)


class EfileRowRemoveView(EfileRowView):
    template_name = "forms_engine/efile_row_remove.html"

    def get(self, request: HttpRequest, pk: int, row_id: int) -> HttpResponse:
        row = self.row(request, pk, row_id)
        return self.render(
            request, bulk=row.bulk, row=row, name=worker_list.display_name(row.worker)[0]
        )

    def post(self, request: HttpRequest, pk: int, row_id: int) -> HttpResponse:
        row = self.row(request, pk, row_id)
        bulk = row.bulk
        if not consume_once_token(request):
            return self.already_submitted(request, detail_url(bulk, "rows"))
        try:
            bulk_filings.remove_row(row, by=_actor(request))
        except ValidationError as error:
            messages.error(request, " ".join(error.messages))
        else:
            messages.success(request, _("Removed the row from the bulk notification."))
        return redirect(detail_url(bulk, "rows"))


class EfileFileDownloadView(EfileView):
    """作った xlsm（記入済み）。行のすべての外国人を見られる人だけ。"""

    def get(self, request: HttpRequest, file_id: int) -> FileResponse:
        record = get_object_or_404(BulkFilingFile.objects.select_related("bulk"), pk=file_id)
        if not bulk_filings.can_download(request.user, record):
            raise PermissionDenied
        name = bulk_filings.filename(
            record.bulk, record.part, record.parts, timezone.localdate(record.created_at)
        )
        response = FileResponse(record.file.open("rb"), as_attachment=True, filename=name)
        bulk_filings.record_download(record, request.user)
        return response
