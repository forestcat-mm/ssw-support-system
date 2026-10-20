"""一括申請の画面（T48・T49。ADR-0039）。操作は forms.generate。

作る → 外国人を足す → 行ごとに申請ごとの入力 → 検査 → 生成 → ダウンロード（記入済み・監査ログ）。
犯罪の申告は、その外国人の担当者と管理者だけが入力・確認できる（一覧には出さない）。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import QuerySet
from django.http import FileResponse, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views import View

from apps.core.models import Action, User
from apps.core.permissions import ActionRequiredMixin
from apps.core.web_forms import consume_once_token, once_context
from apps.forms_engine.bulk_forms import AddWorkersForm, BulkApplicationForm, EntryForm
from apps.forms_engine.models import (
    BulkApplication,
    BulkApplicationEntry,
    BulkApplicationFile,
    SubmissionBundle,
)
from apps.workers.models import ForeignWorker
from services import (
    audit,
    bulk_application_data,
    bulk_applications,
    form_templates,
    worker_list,
    workers,
)


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)
    return request.user


class BulkView(ActionRequiredMixin, View):
    required_action = Action.FORMS_GENERATE
    template_name = ""

    def render(self, request: HttpRequest, **context: Any) -> HttpResponse:
        return TemplateResponse(request, self.template_name, {**context, **once_context(request)})

    def application(self, pk: int) -> BulkApplication:
        return get_object_or_404(BulkApplication.objects.select_related("intermediary"), pk=pk)

    def already_submitted(self, request: HttpRequest, url: str) -> HttpResponse:
        messages.info(request, _("This form has already been submitted."))
        return redirect(url)


def detail_url(application: BulkApplication, anchor: str = "") -> str:
    return reverse("forms:bulk", args=[application.pk]) + (f"#{anchor}" if anchor else "")


class BulkListView(BulkView):
    template_name = "forms_engine/bulk_list.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        applications = BulkApplication.objects.select_related("intermediary").prefetch_related(
            "entries", "files"
        )
        return self.render(request, applications=applications)


class BulkCreateView(BulkView):
    template_name = "forms_engine/bulk_form.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        return self.render(request, form=BulkApplicationForm())

    def post(self, request: HttpRequest) -> HttpResponse:
        if not consume_once_token(request):
            return self.already_submitted(request, reverse("forms:bulk_list"))
        form = BulkApplicationForm(request.POST)
        if not form.is_valid():
            return self.render(request, form=form)
        application = form.save(commit=False)
        application.created_by = application.updated_by = _actor(request)
        application.save()
        messages.success(request, _("Created the bulk application. Add foreign workers."))
        return redirect(detail_url(application))


def candidates(application: BulkApplication) -> QuerySet[ForeignWorker]:
    """足せる外国人（無効化していない・まだ入っていない・基準日に雇用契約がある）。"""
    existing = application.entries.values_list("worker_id", flat=True)
    return (
        workers.active_workers()
        .exclude(pk__in=existing)
        .filter(employment_contracts__isnull=False)
        .distinct()
        .order_by("name_romaji")
    )


class BulkDetailView(BulkView):
    template_name = "forms_engine/bulk_detail.html"

    def page(
        self, request: HttpRequest, application: BulkApplication, **extra: Any
    ) -> HttpResponse:
        entries = bulk_applications.entries_of(application)
        try:
            problems = bulk_applications.check(application)
            not_loaded = False
        except form_templates.FormTemplateError:
            problems, not_loaded = [], True
        by_entry: dict[int, list[bulk_applications.Problem]] = defaultdict(list)
        for problem in problems:
            by_entry[problem.entry.pk].append(problem)
        set_id = bulk_applications.set_id_of(application)
        bundles = {
            bundle.worker_id: bundle
            for bundle in SubmissionBundle.objects.filter(
                worker_id__in=[entry.worker_id for entry in entries], set_id=set_id
            ).order_by("pk")
        }
        query = urlencode({"set_id": set_id, "basis_on": application.basis_on.isoformat()})
        rows = [
            {
                "entry": entry,
                "name": worker_list.display_name(entry.worker)[0],
                "contract": bulk_application_data.contract_of(entry.worker, application.basis_on),
                "problems": by_entry.get(entry.pk, []),
                "bundle": bundles.get(entry.worker_id),
            }
            for entry in entries
        ]
        files = [
            (record, bulk_applications.can_download(request.user, record))
            for record in application.files.select_related("template")
        ]
        choices = [
            (worker.pk, f"{worker_list.display_name(worker)[0]}（{worker.name_romaji}）")
            for worker in candidates(application)
        ]
        audit.record_view(
            application, context={"shown": {"workers": [e.worker_id for e in entries]}}
        )
        return self.render(
            request,
            application=application,
            rows=rows,
            problem_count=len(problems),
            not_loaded=not_loaded,
            query=query,
            files=files,
            add_form=extra.get("add_form") or AddWorkersForm(choices=choices),
        )

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        return self.page(request, self.application(pk))

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """生成（検査を通ったら）。"""
        application = self.application(pk)
        if not consume_once_token(request):
            return self.already_submitted(request, detail_url(application))
        try:
            created = bulk_applications.generate(application, by=_actor(request))
        except bulk_applications.GenerationBlocked as error:
            if error.problems:
                messages.error(
                    request,
                    _("Not generated: fix the %(count)s problem(s) shown for each worker.")
                    % {"count": len(error.problems)},
                )
            else:
                messages.error(request, _("Add foreign workers first."))
            return redirect(detail_url(application, "rows"))
        messages.success(request, _("Generated %(count)s file(s).") % {"count": len(created)})
        return redirect(detail_url(application, "files"))


class BulkAddWorkersView(BulkView):
    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        application = self.application(pk)
        if not consume_once_token(request):
            return self.already_submitted(request, detail_url(application))
        allowed = {worker.pk: worker for worker in candidates(application)}
        chosen = [
            allowed[int(value)]
            for value in request.POST.getlist("workers")
            if value.isdigit() and int(value) in allowed
        ]
        added = bulk_applications.add_workers(application, chosen, by=_actor(request))
        messages.success(request, _("Added %(count)s worker(s).") % {"count": len(added)})
        return redirect(detail_url(application, "rows"))


class BulkEntryView(BulkView):
    """1 人の申請ごとの入力。犯罪の申告は担当者と管理者だけに出す。"""

    template_name = "forms_engine/bulk_entry.html"

    def entry(self, pk: int, entry_id: int) -> BulkApplicationEntry:
        return get_object_or_404(
            BulkApplicationEntry.objects.select_related(
                "application", "worker__assigned_staff", "worker__nationality"
            ),
            pk=entry_id,
            application_id=pk,
        )

    def form(
        self, request: HttpRequest, entry: BulkApplicationEntry, data: Any = None
    ) -> EntryForm:
        template = bulk_applications.template_of(entry.application)
        return EntryForm(
            data,
            entry=entry,
            definition=template.mapping.definition,
            show_sensitive=bulk_applications.can_see_sensitive(request.user, entry.worker),
        )

    def page(
        self, request: HttpRequest, entry: BulkApplicationEntry, form: EntryForm
    ) -> HttpResponse:
        audit.record_view(
            entry.worker,
            context={"bulk_application": entry.application_id, "sensitive": form.show_sensitive},
        )
        return self.render(
            request,
            application=entry.application,
            entry=entry,
            name=worker_list.display_name(entry.worker)[0],
            form=form,
        )

    def get(self, request: HttpRequest, pk: int, entry_id: int) -> HttpResponse:
        entry = self.entry(pk, entry_id)
        return self.page(request, entry, self.form(request, entry))

    def post(self, request: HttpRequest, pk: int, entry_id: int) -> HttpResponse:
        entry = self.entry(pk, entry_id)
        if not consume_once_token(request):
            return self.already_submitted(request, detail_url(entry.application, "rows"))
        form = self.form(request, entry, request.POST)
        if form.is_valid():
            entry.agent = form.cleaned_data.get("agent")
            try:
                bulk_applications.save_inputs(
                    entry, form.inputs(), sensitive=form.sensitive(), by=_actor(request)
                )
                entry.save(update_fields=["agent", "updated_at"])
            except ValidationError as error:
                for name, field_messages in error.message_dict.items():
                    for message in field_messages:
                        form.add_error(name if name in form.fields else None, message)
            else:
                messages.success(request, _("Saved."))
                return redirect(detail_url(entry.application, "rows"))
        return self.page(request, entry, form)


class BulkEntryRemoveView(BulkEntryView):
    template_name = "forms_engine/bulk_entry_remove.html"

    def get(self, request: HttpRequest, pk: int, entry_id: int) -> HttpResponse:
        entry = self.entry(pk, entry_id)
        return self.render(
            request,
            application=entry.application,
            entry=entry,
            name=worker_list.display_name(entry.worker)[0],
        )

    def post(self, request: HttpRequest, pk: int, entry_id: int) -> HttpResponse:
        entry = self.entry(pk, entry_id)
        application = entry.application
        if not consume_once_token(request):
            return self.already_submitted(request, detail_url(application, "rows"))
        entry.delete()
        messages.success(request, _("Removed the worker from the bulk application."))
        return redirect(detail_url(application, "rows"))


class BulkFileDownloadView(BulkView):
    """作った xlsm（記入済み）。犯罪の申告を含めば担当者と管理者だけ。"""

    def get(self, request: HttpRequest, file_id: int) -> FileResponse:
        record = get_object_or_404(
            BulkApplicationFile.objects.select_related("application"), pk=file_id
        )
        if not bulk_applications.can_download(request.user, record):
            raise PermissionDenied
        name = bulk_applications.filename(record.application, record.part, record.parts)
        response = FileResponse(record.file.open("rb"), as_attachment=True, filename=name)
        bulk_applications.record_download(record, request.user)
        return response
