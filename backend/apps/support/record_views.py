"""支援実績の画面（第5章 F05、T18）。「支援」タブの「記録」: 一覧・入力・詳細・取消・添付。

閲覧は外国人の閲覧として監査ログに 1 件残す（WorkerDetailView）。作成・取消・添付の追加と取消は
保存のシグナルで残る。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.models import Action, Attachment, User
from apps.core.web_forms import consume_once_token
from apps.filings.models import Filing
from apps.support.models import PeriodicInterview, SupportRecord
from apps.support.record_forms import (
    AttachmentForm,
    InterviewEditor,
    ReasonForm,
    RecordFilterForm,
    RecordForm,
)
from apps.workers.models import ForeignWorker
from apps.workers.views import WorkerDetailView, WorkerFormView
from services import attachments, authorization, support_interviews, support_records, workers
from services.support_catalog import SupportItem

PAGE_SIZE = 50


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)
    return request.user


def records_url(worker: ForeignWorker) -> str:
    return reverse("workers:support_records", args=[worker.pk])


def record_url(record: SupportRecord) -> str:
    return reverse("workers:support_record", args=[record.worker_id, record.pk])


class RecordTabMixin:
    """「支援」タブの「記録」の共通部分（記録・添付を足せるか）。"""

    request: HttpRequest
    worker: ForeignWorker

    def can_manage_support(self) -> bool:
        return workers.is_active(self.worker) and authorization.has_action(
            self.request.user, Action.SUPPORT_MANAGE
        )


class RecordListView(RecordTabMixin, WorkerDetailView):
    """記録の一覧（新しい順。種別・期間・担当者で絞り込む）。取り消した記録は打ち消して出す。"""

    tab = "support"
    template_name = "support/worker_records.html"

    def tab_context(self, context: dict[str, Any]) -> dict[str, Any]:
        form = RecordFilterForm(self.request.GET or None)
        filters: dict[str, Any] = {}
        if form.is_valid():
            data = form.cleaned_data
            filters = {
                "kind": data["kind"],
                "date_from": data["date_from"],
                "date_to": data["date_to"],
                "staff_id": data["staff"].pk if data["staff"] else None,
            }
        queryset = support_records.filter_records(
            support_records.records_of(self.worker), **filters
        ).prefetch_related("attachments")
        page = Paginator(queryset, PAGE_SIZE).get_page(self.request.GET.get("page"))
        query = {k: v for k, v in self.request.GET.items() if k != "page" and v}
        context.update(
            filter_form=form,
            page=page,
            records=[
                (record, support_records.details_display(record)) for record in page.object_list
            ],
            filtered=any(filters.values()),
            page_query=urlencode(query),
            record_kinds=support_records.RECORD_KINDS,
            can_manage_support=self.can_manage_support(),
            support_subtab="records",
        )
        return {
            "shown": {"support_records": [record.pk for record in page.object_list]},
            "conditions": {k: str(v) for k, v in filters.items() if v},
        }


class RecordDetailView(RecordTabMixin, WorkerDetailView):
    """記録 1 件（本文・種別ごとの欄・添付）。"""

    tab = "support"
    template_name = "support/record_detail.html"

    def tab_context(self, context: dict[str, Any]) -> dict[str, Any]:
        record = get_object_or_404(
            support_records.records_of(self.worker), pk=self.kwargs["record_id"]
        )
        files = list(record.attachments.select_related("created_by", "voided_by").order_by("pk"))
        context.update(
            record=record,
            details=support_records.details_display(record),
            files=files,
            can_manage_support=self.can_manage_support(),
            can_change_record=self.can_manage_support() and record.voided_at is None,
            support_subtab="records",
        )
        return {"shown": {"support_record": record.pk, "attachments": [f.pk for f in files]}}


class RecordFormView(WorkerFormView):
    """記録の画面（作成・取消・添付）の共通部分。権限は support.manage。"""

    required_action = Action.SUPPORT_MANAGE

    def record(self, worker: ForeignWorker, record_id: int) -> SupportRecord:
        """取り消していない記録（取り消した記録は変更しない）。"""
        return get_object_or_404(SupportRecord, pk=record_id, worker=worker, voided_at__isnull=True)

    def single_record(self, worker: ForeignWorker, record_id: int) -> SupportRecord:
        """1 行ずつ取り消せる記録（定期面談の行は組ごとに取り消す）。"""
        record = self.record(worker, record_id)
        if record.interview_id is not None:
            raise Http404
        return record


class RecordCreateView(RecordFormView):
    """種別ごとの入力画面（共通の欄＋種別ごとの最小限の欄＋添付）。"""

    template_name = "support/record_form.html"

    def kind(self, value: str) -> SupportItem:
        kind = support_records.kind_or_none(value)
        if kind is None:
            raise Http404
        return kind

    def page(self, request: HttpRequest, worker: ForeignWorker, form: RecordForm) -> HttpResponse:
        return self.render(
            request,
            worker=worker,
            form=form,
            kind=form.kind,
            title=_("Record: %(kind)s") % {"kind": form.kind.label},
            back_url=records_url(worker),
        )

    def get(self, request: HttpRequest, pk: int, kind: str) -> HttpResponse:
        worker = self.worker(request, pk)
        form = RecordForm(
            kind=self.kind(kind), initial=RecordForm.initial_for(worker, timezone.localdate())
        )
        return self.page(request, worker, form)

    def post(self, request: HttpRequest, pk: int, kind: str) -> HttpResponse:
        worker = self.worker(request, pk)
        item = self.kind(kind)
        if not consume_once_token(request):
            return self.already_submitted(request, records_url(worker))
        form = RecordForm(request.POST, request.FILES, kind=item)
        if form.is_valid():
            try:
                record = support_records.create_record(
                    worker,
                    form.to_input(),
                    by=_actor(request),
                    files=form.cleaned_data["uploads"],
                )
            except ValidationError as error:
                form.apply_errors(error)
            else:
                messages.success(request, _("Saved the support record."))
                return redirect(record_url(record))
        return self.page(request, worker, form)


class RecordVoidView(RecordFormView):
    """記録の取消（確認画面。理由は必須）。"""

    template_name = "support/record_void.html"

    def form(self, data: Any = None) -> ReasonForm:
        return ReasonForm(data, help_text=_("Example: the date was entered incorrectly."))

    def get(self, request: HttpRequest, pk: int, record_id: int) -> HttpResponse:
        worker = self.worker(request, pk)
        record = self.single_record(worker, record_id)
        return self.render(
            request,
            worker=worker,
            record=record,
            details=support_records.details_display(record),
            form=self.form(),
        )

    def post(self, request: HttpRequest, pk: int, record_id: int) -> HttpResponse:
        worker = self.worker(request, pk)
        if not consume_once_token(request):
            return self.already_submitted(request, records_url(worker))
        record = self.single_record(worker, record_id)
        form = self.form(request.POST)
        if form.is_valid():
            try:
                support_records.void_record(
                    record, by=_actor(request), reason=form.cleaned_data["reason"]
                )
            except ValidationError as error:
                form.add_error("reason", error)
            else:
                messages.success(request, _("Voided the support record."))
                return redirect(records_url(worker))
        return self.render(
            request,
            worker=worker,
            record=record,
            details=support_records.details_display(record),
            form=form,
        )


class AttachmentAddView(RecordFormView):
    """記録に添付を足す（署名入りの確認書・写真など）。"""

    template_name = "support/record_attach.html"

    def get(self, request: HttpRequest, pk: int, record_id: int) -> HttpResponse:
        worker = self.worker(request, pk)
        record = self.record(worker, record_id)
        return self.render(request, worker=worker, record=record, form=AttachmentForm())

    def post(self, request: HttpRequest, pk: int, record_id: int) -> HttpResponse:
        worker = self.worker(request, pk)
        record = self.record(worker, record_id)
        if not consume_once_token(request):
            return self.already_submitted(request, record_url(record))
        form = AttachmentForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                support_records.add_attachments(
                    record, form.cleaned_data["uploads"], by=_actor(request)
                )
            except ValidationError as error:
                if hasattr(error, "error_dict"):
                    for messages_ in error.error_dict.values():
                        form.add_error("uploads", messages_)
                else:
                    form.add_error(None, error)
            else:
                messages.success(request, _("Added the attachments."))
                return redirect(record_url(record))
        return self.render(request, worker=worker, record=record, form=form)


class AttachmentVoidView(RecordFormView):
    """添付の取消（確認画面。理由は必須。ファイルは消さずに打ち消して残す）。"""

    template_name = "support/attachment_void.html"

    def target(
        self, request: HttpRequest, pk: int, record_id: int, attachment_id: int
    ) -> tuple[ForeignWorker, SupportRecord, Attachment]:
        worker = self.worker(request, pk)
        record = self.record(worker, record_id)
        attachment = get_object_or_404(
            attachments.active(record.attachments.all()), pk=attachment_id
        )
        return worker, record, attachment

    def form(self, data: Any = None) -> ReasonForm:
        return ReasonForm(data, help_text=_("Example: a document of another person was attached."))

    def get(
        self, request: HttpRequest, pk: int, record_id: int, attachment_id: int
    ) -> HttpResponse:
        worker, record, attachment = self.target(request, pk, record_id, attachment_id)
        return self.render(
            request, worker=worker, record=record, attachment=attachment, form=self.form()
        )

    def post(
        self, request: HttpRequest, pk: int, record_id: int, attachment_id: int
    ) -> HttpResponse:
        worker, record, attachment = self.target(request, pk, record_id, attachment_id)
        if not consume_once_token(request):
            return self.already_submitted(request, record_url(record))
        form = self.form(request.POST)
        if form.is_valid():
            try:
                attachments.void_attachment(
                    attachment, by=_actor(request), reason=form.cleaned_data["reason"]
                )
            except ValidationError as error:
                form.add_error("reason", error)
            else:
                messages.success(request, _("Voided the attachment."))
                return redirect(record_url(record))
        return self.render(request, worker=worker, record=record, attachment=attachment, form=form)


# --- 定期面談（T19）---------------------------------------------------------------------


def interview_url(interview: PeriodicInterview) -> str:
    return reverse("workers:support_interview", args=[interview.worker_id, interview.pk])


class InterviewCreateView(RecordFormView):
    """定期面談の記録（本人用・上司用の 2 行と組の欄を 1 画面で）。

    ?filing= で完了にするタスクを指定する（ダッシュボード・タスクの詳細から）。
    """

    template_name = "support/interview_form.html"

    def task(self, request: HttpRequest, worker: ForeignWorker) -> Filing | None:
        value = request.GET.get("filing") or request.POST.get("filing") or ""
        if not value.isdigit():
            return None
        return support_interviews.open_task(worker, Filing(pk=int(value)))

    def page(
        self, request: HttpRequest, worker: ForeignWorker, editor: InterviewEditor, task: Any
    ) -> HttpResponse:
        return self.render(
            request, worker=worker, editor=editor, task=task, back_url=records_url(worker)
        )

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        worker = self.worker(request, pk)
        return self.page(request, worker, InterviewEditor(worker), self.task(request, worker))

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        worker = self.worker(request, pk)
        if not consume_once_token(request):
            return self.already_submitted(request, records_url(worker))
        task = self.task(request, worker)
        editor = InterviewEditor(worker, request.POST, request.FILES)
        if editor.is_valid():
            try:
                interview = support_interviews.create_interview(
                    worker, editor.to_input(), by=_actor(request), files=editor.files(), task=task
                )
            except ValidationError as error:
                editor.apply_errors(error)
            else:
                if interview.completed_filing_id is not None:
                    messages.success(
                        request, _("Saved the periodic interview and completed the task.")
                    )
                else:
                    messages.success(request, _("Saved the periodic interview."))
                return redirect(interview_url(interview))
        return self.page(request, worker, editor, task)


class InterviewDetailView(RecordTabMixin, WorkerDetailView):
    """定期面談の組（本人用・上司用の 2 行・通報の欄・タスク）。"""

    tab = "support"
    template_name = "support/interview_detail.html"

    def tab_context(self, context: dict[str, Any]) -> dict[str, Any]:
        interview = get_object_or_404(
            support_interviews.interviews_of(self.worker).select_related(
                "completed_filing", "next_filing", "voided_by"
            ),
            pk=self.kwargs["interview_id"],
        )
        records = sorted(interview.records.all(), key=lambda r: r.counterpart != "worker")
        context.update(
            interview=interview,
            interview_records=records,
            interview_warnings=support_interviews.warnings(interview),
            can_void_interview=self.can_manage_support() and interview.voided_at is None,
            support_subtab="records",
        )
        return {
            "shown": {
                "periodic_interview": interview.pk,
                "support_records": [record.pk for record in records],
            }
        }


class InterviewVoidView(RecordFormView):
    """定期面談の取消（組ごと。理由は必須）。タスクに手が付いていなければ自動で戻す。"""

    template_name = "support/interview_void.html"

    def interview(self, worker: ForeignWorker, interview_id: int) -> PeriodicInterview:
        return get_object_or_404(
            support_interviews.interviews_of(worker), pk=interview_id, voided_at__isnull=True
        )

    def form(self, data: Any = None) -> ReasonForm:
        return ReasonForm(data, help_text=_("Example: the interview of another person."))

    def get(self, request: HttpRequest, pk: int, interview_id: int) -> HttpResponse:
        worker = self.worker(request, pk)
        interview = self.interview(worker, interview_id)
        return self.render(request, worker=worker, interview=interview, form=self.form())

    def post(self, request: HttpRequest, pk: int, interview_id: int) -> HttpResponse:
        worker = self.worker(request, pk)
        if not consume_once_token(request):
            return self.already_submitted(request, records_url(worker))
        interview = self.interview(worker, interview_id)
        form = self.form(request.POST)
        if form.is_valid():
            try:
                result = support_interviews.void_interview(
                    interview, by=_actor(request), reason=form.cleaned_data["reason"]
                )
            except ValidationError as error:
                form.add_error("reason", error)
            else:
                if result.tasks_reverted:
                    messages.success(
                        request, _("Voided the periodic interview and put the tasks back.")
                    )
                else:
                    messages.warning(
                        request,
                        _(
                            "Voided the periodic interview. The tasks were already changed, "
                            "so they were left as they are. Check the periodic interview tasks."
                        ),
                    )
                return redirect(interview_url(interview))
        return self.render(request, worker=worker, interview=interview, form=form)
