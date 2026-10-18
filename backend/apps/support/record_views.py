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
from django.db import transaction
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.generic import TemplateView

from apps.contracts.models import EmploymentContract
from apps.core.models import Action, Attachment, User
from apps.core.permissions import ActionRequiredMixin
from apps.core.web_forms import consume_once_token
from apps.filings.models import Filing
from apps.forms_engine.forms import NOT_LOADED, BriefingGenerateForm
from apps.forms_engine.models import GeneratedDocument
from apps.support.models import (
    Briefing,
    Consultation,
    DeliveryKind,
    DocumentDelivery,
    PeriodicInterview,
    SupportRecord,
)
from apps.support.record_forms import (
    AttachmentForm,
    BriefingEditor,
    ConfirmationForm,
    ConsultationFilterForm,
    DeliveryForm,
    EntryForm,
    InterviewEditor,
    ReasonForm,
    ReceptionForm,
    RecordFilterForm,
    RecordForm,
)
from apps.workers.models import ForeignWorker
from apps.workers.views import WorkerDetailView, WorkerFormView
from services import (
    attachments,
    audit,
    authorization,
    briefings,
    consultations,
    deliveries,
    form_checks,
    form_data_briefing,
    form_data_interview,
    form_generation,
    form_templates,
    incident_reports,
    interview_answers,
    support_interviews,
    support_records,
    workers,
)
from services.support_catalog import SupportItem

PAGE_SIZE = 50


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)
    return request.user


def records_url(worker: ForeignWorker) -> str:
    return reverse("workers:support_records", args=[worker.pk])


def record_url(record: SupportRecord) -> str:
    return reverse("workers:support_record", args=[record.worker_id, record.pk])


def incident_messages(request: HttpRequest, keys: list[str]) -> None:
    """記録から起票した第4-3号（特異事案報告）のタスクを知らせる（T24a）。"""
    for filing in incident_reports.tasks_for(keys):
        messages.info(
            request,
            _("Linked this record to the report task (form 4-3) due on %(due)s.")
            % {"due": filing.due_on.isoformat()},
        )


def withdrawn_messages(request: HttpRequest, result: incident_reports.Withdrawn) -> None:
    """取消で第4-3号のタスクの参照を外した結果を知らせる（T24a）。"""
    if result.cancelled:
        messages.info(request, _("Cancelled the report task (form 4-3) raised by this record."))
    if result.kept:
        messages.info(
            request,
            _(
                "Removed this record from the report task (form 4-3). The task stays because "
                "other records are linked to it."
            ),
        )
    if result.needs_attention:
        messages.warning(
            request,
            _(
                "The report task (form 4-3) raised by this record was already being handled, "
                "so it was left as it is. Check the task and correct it by hand."
            ),
        )


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
            briefing_kinds=briefings.BRIEFING_KINDS,
            signed_record_ids={
                record.pk
                for record in page.object_list
                if hasattr(record, "briefing")
                and briefings.is_signed(record.briefing, record.attachments.all())
            },
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
        """1 行ずつ取り消せる記録（面談・相談・事前ガイダンス等は、それぞれの画面で取り消す）。"""
        record = self.record(worker, record_id)
        if (
            record.interview_id is not None
            or hasattr(record, "consultation")
            or hasattr(record, "briefing")
        ):
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
                incident_messages(request, [incident_reports.record_key(record)])
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
                withdrawn = support_records.void_record(
                    record, by=_actor(request), reason=form.cleaned_data["reason"]
                )
            except ValidationError as error:
                form.add_error("reason", error)
            else:
                messages.success(request, _("Voided the support record."))
                withdrawn_messages(request, withdrawn)
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
                incident_messages(request, incident_reports.interview_keys(interview))
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
        answers = []
        for record in records:
            answer_set = interview_answers.answers_of(record)
            rows = (
                interview_answers.rows_of(answer_set, supervisor=record.counterpart != "worker")
                if answer_set is not None
                else []
            )
            answers.append((record, answer_set, rows))
        context.update(
            interview=interview,
            interview_records=records,
            interview_answers=answers,
            nonconformity=interview_answers.nonconformity_of(interview),
            can_generate_reports=(
                interview.voided_at is None
                and workers.is_active(self.worker)
                and authorization.has_action(self.request.user, Action.FORMS_GENERATE)
            ),
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
                withdrawn_messages(request, result.incident)
                return redirect(interview_url(interview))
        return self.render(request, worker=worker, interview=interview, form=form)


# --- 相談・苦情対応（T20）-----------------------------------------------------------------


def consultation_url(consultation: Consultation) -> str:
    return reverse("workers:support_consultation", args=[consultation.worker_id, consultation.pk])


class ConsultationCreateView(RecordFormView):
    """相談の受付（共通の欄＋主な分類・対応状況）。"""

    template_name = "support/consultation_form.html"

    def page(
        self, request: HttpRequest, worker: ForeignWorker, form: ReceptionForm
    ) -> HttpResponse:
        return self.render(request, worker=worker, form=form, back_url=records_url(worker))

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        worker = self.worker(request, pk)
        initial = RecordForm.initial_for(worker, timezone.localdate())
        return self.page(request, worker, ReceptionForm(initial=initial))

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        worker = self.worker(request, pk)
        if not consume_once_token(request):
            return self.already_submitted(request, records_url(worker))
        form = ReceptionForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                consultation = consultations.create_consultation(
                    worker,
                    form.to_reception(),
                    by=_actor(request),
                    files=form.cleaned_data["uploads"],
                )
            except ValidationError as error:
                form.apply_errors(error)
            else:
                messages.success(request, _("Received the consultation."))
                return redirect(consultation_url(consultation))
        return self.page(request, worker, form)


class ConsultationMixin:
    request: HttpRequest

    def consultation_of(self, worker: ForeignWorker, consultation_id: int) -> Consultation:
        return get_object_or_404(consultations.consultations(), pk=consultation_id, worker=worker)


class ConsultationDetailView(ConsultationMixin, RecordTabMixin, WorkerDetailView):
    """相談の案件（受付の内容と経過の時系列）。"""

    tab = "support"
    template_name = "support/consultation_detail.html"

    def tab_context(self, context: dict[str, Any]) -> dict[str, Any]:
        consultation = self.consultation_of(self.worker, self.kwargs["consultation_id"])
        items = consultations.timeline(consultation)
        open_case = self.can_manage_support() and consultation.voided_at is None
        context.update(
            consultation=consultation,
            timeline=items,
            can_add_entry=open_case,
            support_subtab="records",
        )
        return {
            "shown": {
                "consultation": consultation.pk,
                "support_record": consultation.record_id,
                "consultation_entries": [item.entry.pk for item in items if item.entry],
            }
        }


class ConsultationEntryView(ConsultationMixin, RecordFormView):
    """経過の追記（これまでの経過を上に出す）。"""

    template_name = "support/consultation_entry.html"

    def target(self, request: HttpRequest, pk: int, consultation_id: int) -> Any:
        worker = self.worker(request, pk)
        consultation = self.consultation_of(worker, consultation_id)
        if consultation.voided_at is not None:
            raise Http404
        return worker, consultation

    def page(self, request: HttpRequest, worker: Any, consultation: Any, form: Any) -> HttpResponse:
        return self.render(
            request,
            worker=worker,
            consultation=consultation,
            timeline=consultations.timeline(consultation),
            form=form,
        )

    def get(self, request: HttpRequest, pk: int, consultation_id: int) -> HttpResponse:
        worker, consultation = self.target(request, pk, consultation_id)
        form = EntryForm(initial=EntryForm.initial_for(consultation, timezone.localdate()))
        return self.page(request, worker, consultation, form)

    def post(self, request: HttpRequest, pk: int, consultation_id: int) -> HttpResponse:
        worker, consultation = self.target(request, pk, consultation_id)
        if not consume_once_token(request):
            return self.already_submitted(request, consultation_url(consultation))
        form = EntryForm(request.POST)
        if form.is_valid():
            try:
                entry = consultations.add_entry(consultation, form.to_input(), by=_actor(request))
            except ValidationError as error:
                form.apply_errors(error)
            else:
                messages.success(request, _("Added the entry."))
                incident_messages(
                    request,
                    [
                        incident_reports.entry_report_key(entry),
                        incident_reports.entry_hello_work_key(entry),
                    ],
                )
                return redirect(consultation_url(consultation))
        return self.page(request, worker, consultation, form)


class ConsultationVoidView(ConsultationMixin, RecordFormView):
    """案件の取消（理由は必須）。経過は残る。"""

    template_name = "support/consultation_void.html"

    def form(self, data: Any = None) -> ReasonForm:
        return ReasonForm(data, help_text=_("Example: the consultation of another person."))

    def target(self, request: HttpRequest, pk: int, consultation_id: int) -> Any:
        worker = self.worker(request, pk)
        consultation = self.consultation_of(worker, consultation_id)
        if consultation.voided_at is not None:
            raise Http404
        return worker, consultation

    def get(self, request: HttpRequest, pk: int, consultation_id: int) -> HttpResponse:
        worker, consultation = self.target(request, pk, consultation_id)
        return self.render(request, worker=worker, consultation=consultation, form=self.form())

    def post(self, request: HttpRequest, pk: int, consultation_id: int) -> HttpResponse:
        worker, consultation = self.target(request, pk, consultation_id)
        if not consume_once_token(request):
            return self.already_submitted(request, consultation_url(consultation))
        form = self.form(request.POST)
        if form.is_valid():
            try:
                withdrawn = consultations.void_consultation(
                    consultation, by=_actor(request), reason=form.cleaned_data["reason"]
                )
            except ValidationError as error:
                form.add_error("reason", error)
            else:
                messages.success(request, _("Voided the consultation."))
                withdrawn_messages(request, withdrawn)
                return redirect(consultation_url(consultation))
        return self.render(request, worker=worker, consultation=consultation, form=form)


class ConsultationListView(ActionRequiredMixin, TemplateView):
    """全員分の相談の一覧（既定は未解決）。一覧の閲覧として監査ログに 1 件残す。"""

    required_action = Action.SUPPORT_VIEW
    template_name = "support/consultation_list.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        form = ConsultationFilterForm(self.request.GET or None)
        statuses = consultations.OPEN_STATUSES
        conditions: dict[str, Any] = {}
        filters: dict[str, Any] = {}
        if form.is_valid():
            data = form.cleaned_data
            statuses = form.statuses()
            filters = {
                "category": data["category"],
                "staff_id": data["staff"].pk if data["staff"] else None,
                "date_from": data["date_from"],
                "date_to": data["date_to"],
            }
            conditions = {"status": data["status"] or "open", **filters}
        queryset = consultations.filter_consultations(
            authorization.scope(self.request.user, consultations.consultations()),
            statuses=statuses,
            **filters,
        )
        page = Paginator(queryset, PAGE_SIZE).get_page(self.request.GET.get("page"))
        query = {k: v for k, v in self.request.GET.items() if k != "page" and v}
        audit.record_list_view(
            "consultations:list",
            conditions=conditions or {"status": "open"},
            object_ids=[item.pk for item in page.object_list],
        )
        context.update(
            form=form,
            page=page,
            filtered=bool(query),
            page_query=urlencode(query),
        )
        return context


# --- 事前ガイダンス・生活オリエンテーション（T21）---------------------------------------------


def briefing_url(briefing: Briefing) -> str:
    return reverse("workers:support_briefing", args=[briefing.worker_id, briefing.pk])


class BriefingMixin:
    def briefing_of(self, worker: ForeignWorker, briefing_id: int) -> Briefing:
        return get_object_or_404(briefings.briefings_of(worker), pk=briefing_id)

    def open_briefing(self, worker: ForeignWorker, briefing_id: int) -> Briefing:
        """取り消していない記録（取り消した記録には添付・取消をしない）。"""
        briefing = self.briefing_of(worker, briefing_id)
        if briefing.record.voided_at is not None:
            raise Http404
        return briefing


class BriefingCreateView(RecordFormView):
    """事前ガイダンス・生活オリエンテーションの記録（実施日時の行・チェックリスト・費用・署名済みの確認書）。"""

    template_name = "support/briefing_form.html"

    def kind(self, value: str) -> SupportItem:
        kind = next((kind for kind in briefings.BRIEFING_KINDS if kind.value == value), None)
        if kind is None:
            raise Http404
        return kind

    def page(
        self, request: HttpRequest, worker: ForeignWorker, editor: BriefingEditor
    ) -> HttpResponse:
        return self.render(
            request,
            worker=worker,
            editor=editor,
            form=editor.form,
            title=_("Record: %(kind)s") % {"kind": editor.kind.label},
            duration=editor.checklist.duration,
            guideline_hours=briefings.hours_text(editor.checklist.duration.guideline_minutes),
            minimum_hours=briefings.hours_text(editor.checklist.duration.minimum_minutes),
            back_url=records_url(worker),
        )

    def get(self, request: HttpRequest, pk: int, kind: str) -> HttpResponse:
        worker = self.worker(request, pk)
        return self.page(request, worker, BriefingEditor(worker, self.kind(kind)))

    def post(self, request: HttpRequest, pk: int, kind: str) -> HttpResponse:
        worker = self.worker(request, pk)
        item = self.kind(kind)
        if not consume_once_token(request):
            return self.already_submitted(request, records_url(worker))
        editor = BriefingEditor(worker, item, request.POST, request.FILES)
        if editor.is_valid():
            try:
                briefing = briefings.create_briefing(
                    worker,
                    editor.to_input(),
                    by=_actor(request),
                    confirmations=editor.confirmations(),
                )
            except ValidationError as error:
                editor.apply_errors(error)
            else:
                messages.success(request, _("Saved the support record."))
                return redirect(briefing_url(briefing))
        return self.page(request, worker, editor)


class BriefingDetailView(BriefingMixin, RecordTabMixin, WorkerDetailView):
    """記録 1 件（実施日時・チェックリスト・費用・署名済みの確認書・警告）。"""

    tab = "support"
    template_name = "support/briefing_detail.html"

    def tab_context(self, context: dict[str, Any]) -> dict[str, Any]:
        briefing = self.briefing_of(self.worker, self.kwargs["briefing_id"])
        record = briefing.record
        files = list(record.attachments.select_related("created_by", "voided_by").order_by("pk"))
        open_record = self.can_manage_support() and record.voided_at is None
        context.update(
            briefing=briefing,
            record=record,
            sessions=list(briefing.sessions.all()),
            total_minutes=briefings.total_minutes(briefing),
            sections=briefings.checklist_states(briefing),
            counts=briefings.check_counts(briefing),
            checklist=briefings.checklist_of(briefing),
            fees=list(briefing.fees.all()),
            briefing_warnings=briefings.warnings(briefing),
            confirmation_kind=briefings.CONFIRMATION_KINDS[SupportItem(record.kind)],
            files=files,
            can_change_record=open_record,
            can_generate_form=record.voided_at is None
            and workers.is_active(self.worker)
            and authorization.has_action(self.request.user, Action.FORMS_GENERATE),
            form_id=BRIEFING_FORM_IDS[SupportItem(record.kind)],
            support_subtab="records",
        )
        return {
            "shown": {
                "briefing": briefing.pk,
                "support_record": record.pk,
                "attachments": [file.pk for file in files],
            }
        }


class BriefingConfirmationView(BriefingMixin, RecordFormView):
    """署名済みの確認書を後から添付する（署名日は必須）。"""

    template_name = "support/briefing_confirmation.html"

    def get(self, request: HttpRequest, pk: int, briefing_id: int) -> HttpResponse:
        worker = self.worker(request, pk)
        briefing = self.open_briefing(worker, briefing_id)
        return self.render(request, worker=worker, briefing=briefing, form=ConfirmationForm())

    def post(self, request: HttpRequest, pk: int, briefing_id: int) -> HttpResponse:
        worker = self.worker(request, pk)
        briefing = self.open_briefing(worker, briefing_id)
        if not consume_once_token(request):
            return self.already_submitted(request, briefing_url(briefing))
        form = ConfirmationForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                briefings.add_confirmation(
                    briefing,
                    form.cleaned_data["confirmation"],
                    signed_on=form.cleaned_data["signed_on"],
                    by=_actor(request),
                )
            except ValidationError as error:
                form.apply_errors(error)
            else:
                messages.success(request, _("Attached the signed confirmation."))
                return redirect(briefing_url(briefing))
        return self.render(request, worker=worker, briefing=briefing, form=form)


class BriefingVoidView(BriefingMixin, RecordFormView):
    """取消（理由は必須）。チェック・実施日時・費用・添付は残る。"""

    template_name = "support/briefing_void.html"

    def form(self, data: Any = None) -> ReasonForm:
        return ReasonForm(data, help_text=_("Example: the record of another person."))

    def get(self, request: HttpRequest, pk: int, briefing_id: int) -> HttpResponse:
        worker = self.worker(request, pk)
        briefing = self.open_briefing(worker, briefing_id)
        return self.render(request, worker=worker, briefing=briefing, form=self.form())

    def post(self, request: HttpRequest, pk: int, briefing_id: int) -> HttpResponse:
        worker = self.worker(request, pk)
        briefing = self.open_briefing(worker, briefing_id)
        if not consume_once_token(request):
            return self.already_submitted(request, briefing_url(briefing))
        form = self.form(request.POST)
        if form.is_valid():
            try:
                briefings.void_briefing(
                    briefing, by=_actor(request), reason=form.cleaned_data["reason"]
                )
            except ValidationError as error:
                form.add_error("reason", error)
            else:
                messages.success(request, _("Voided the support record."))
                return redirect(briefing_url(briefing))
        return self.render(request, worker=worker, briefing=briefing, form=form)


# --- 交付記録（T22）----------------------------------------------------------------------


def deliveries_url(worker: ForeignWorker) -> str:
    return reverse("workers:support_deliveries", args=[worker.pk])


def delivery_url(delivery: DocumentDelivery) -> str:
    return reverse("workers:support_delivery", args=[delivery.worker_id, delivery.pk])


class DeliveryListView(RecordTabMixin, WorkerDetailView):
    """「支援」タブの「交付」: 交付記録の一覧と、契約ごとのキャリアアッププランの判定（飲食）。"""

    tab = "support"
    template_name = "support/worker_deliveries.html"

    def tab_context(self, context: dict[str, Any]) -> dict[str, Any]:
        items = list(deliveries.deliveries_of(self.worker))
        rows = [
            (
                delivery,
                deliveries.has_receipt(delivery, delivery.attachments.all()),
                deliveries.warnings(delivery, delivery.attachments.all()),
            )
            for delivery in items
        ]
        context.update(
            rows=rows,
            findings=deliveries.career_plan_findings(self.worker),
            delivery_kinds=DeliveryKind,
            can_manage_support=self.can_manage_support(),
            support_subtab="deliveries",
        )
        return {"shown": {"document_deliveries": [delivery.pk for delivery in items]}}


class DeliveryMixin:
    def delivery_of(self, worker: ForeignWorker, delivery_id: int) -> DocumentDelivery:
        return get_object_or_404(deliveries.deliveries_of(worker), pk=delivery_id)

    def open_delivery(self, worker: ForeignWorker, delivery_id: int) -> DocumentDelivery:
        """取り消していない記録（取り消した記録には添付・取消をしない）。"""
        delivery = self.delivery_of(worker, delivery_id)
        if delivery.voided_at is not None:
            raise Http404
        return delivery


class DeliveryDetailView(DeliveryMixin, RecordTabMixin, WorkerDetailView):
    """交付記録 1 件（警告・署名済みの控え）。"""

    tab = "support"
    template_name = "support/delivery_detail.html"

    def tab_context(self, context: dict[str, Any]) -> dict[str, Any]:
        delivery = self.delivery_of(self.worker, self.kwargs["delivery_id"])
        files = list(delivery.attachments.select_related("created_by", "voided_by").order_by("pk"))
        context.update(
            delivery=delivery,
            files=files,
            has_receipt=deliveries.has_receipt(delivery, files),
            delivery_warnings=deliveries.warnings(delivery, files),
            can_change_delivery=self.can_manage_support() and delivery.voided_at is None,
            support_subtab="deliveries",
        )
        return {"shown": {"document_delivery": delivery.pk, "attachments": [f.pk for f in files]}}


class DeliveryCreateView(RecordFormView):
    """種別ごとの交付記録の入力（署名済みの控えも添付できる）。"""

    template_name = "support/delivery_form.html"

    def kind(self, value: str) -> str:
        if value not in DeliveryKind.values:
            raise Http404
        return value

    def page(self, request: HttpRequest, worker: ForeignWorker, form: DeliveryForm) -> HttpResponse:
        pk = form["generated"].value()
        generated = (
            GeneratedDocument.objects.select_related("template")
            .filter(pk=pk, worker=worker)
            .first()
            if str(pk or "").isdigit()
            else None
        )
        return self.render(
            request,
            worker=worker,
            form=form,
            generated=generated,
            title=_("Record a delivery: %(kind)s") % {"kind": DeliveryKind(form.kind).label},
            back_url=deliveries_url(worker),
        )

    def get(self, request: HttpRequest, pk: int, kind: str) -> HttpResponse:
        worker = self.worker(request, pk)
        initial = DeliveryForm.initial_for(worker, timezone.localdate())
        generated = request.GET.get("generated", "")
        if generated.isdigit():
            initial["generated"] = int(generated)  # 生成履歴から作る（T31-D16）
            document = GeneratedDocument.objects.filter(pk=int(generated), worker=worker).first()
            if document is not None and isinstance(document.target, EmploymentContract):
                initial["contract"] = document.target.pk  # 自機関の書類は対象の契約（T36）
        form = DeliveryForm(worker=worker, kind=self.kind(kind), initial=initial)
        return self.page(request, worker, form)

    def post(self, request: HttpRequest, pk: int, kind: str) -> HttpResponse:
        worker = self.worker(request, pk)
        value = self.kind(kind)
        if not consume_once_token(request):
            return self.already_submitted(request, deliveries_url(worker))
        form = DeliveryForm(request.POST, request.FILES, worker=worker, kind=value)
        if form.is_valid():
            try:
                delivery = deliveries.create_delivery(
                    worker, form.to_input(), by=_actor(request), files=form.cleaned_data["uploads"]
                )
            except ValidationError as error:
                form.apply_errors(error)
            else:
                messages.success(request, _("Saved the delivery record."))
                return redirect(delivery_url(delivery))
        return self.page(request, worker, form)


class DeliveryVoidView(DeliveryMixin, RecordFormView):
    """交付記録の取消（理由は必須。添付は残る）。"""

    template_name = "support/delivery_void.html"

    def form(self, data: Any = None) -> ReasonForm:
        return ReasonForm(data, help_text=_("Example: the date was entered incorrectly."))

    def get(self, request: HttpRequest, pk: int, delivery_id: int) -> HttpResponse:
        worker = self.worker(request, pk)
        delivery = self.open_delivery(worker, delivery_id)
        return self.render(request, worker=worker, delivery=delivery, form=self.form())

    def post(self, request: HttpRequest, pk: int, delivery_id: int) -> HttpResponse:
        worker = self.worker(request, pk)
        delivery = self.open_delivery(worker, delivery_id)
        if not consume_once_token(request):
            return self.already_submitted(request, delivery_url(delivery))
        form = self.form(request.POST)
        if form.is_valid():
            try:
                deliveries.void_delivery(
                    delivery, by=_actor(request), reason=form.cleaned_data["reason"]
                )
            except ValidationError as error:
                form.add_error("reason", error)
            else:
                messages.success(request, _("Voided the delivery record."))
                return redirect(delivery_url(delivery))
        return self.render(request, worker=worker, delivery=delivery, form=form)


class DeliveryAttachView(DeliveryMixin, RecordFormView):
    """署名済みの控え・受領の記録を足す。"""

    template_name = "support/delivery_attach.html"

    def get(self, request: HttpRequest, pk: int, delivery_id: int) -> HttpResponse:
        worker = self.worker(request, pk)
        delivery = self.open_delivery(worker, delivery_id)
        return self.render(request, worker=worker, delivery=delivery, form=AttachmentForm())

    def post(self, request: HttpRequest, pk: int, delivery_id: int) -> HttpResponse:
        worker = self.worker(request, pk)
        delivery = self.open_delivery(worker, delivery_id)
        if not consume_once_token(request):
            return self.already_submitted(request, delivery_url(delivery))
        form = AttachmentForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                deliveries.add_receipts(delivery, form.cleaned_data["uploads"], by=_actor(request))
            except ValidationError as error:
                if hasattr(error, "error_dict"):
                    for messages_ in error.error_dict.values():
                        form.add_error("uploads", messages_)
                else:
                    form.add_error(None, error)
            else:
                messages.success(request, _("Added the attachments."))
                return redirect(delivery_url(delivery))
        return self.render(request, worker=worker, delivery=delivery, form=form)


class DeliveryAttachmentVoidView(DeliveryMixin, RecordFormView):
    """控えの取消（理由は必須。ファイルは消さずに打ち消して残す）。"""

    template_name = "support/delivery_attachment_void.html"

    def target(
        self, request: HttpRequest, pk: int, delivery_id: int, attachment_id: int
    ) -> tuple[ForeignWorker, DocumentDelivery, Attachment]:
        worker = self.worker(request, pk)
        delivery = self.open_delivery(worker, delivery_id)
        attachment = get_object_or_404(
            attachments.active(delivery.attachments.all()), pk=attachment_id
        )
        return worker, delivery, attachment

    def form(self, data: Any = None) -> ReasonForm:
        return ReasonForm(data, help_text=_("Example: a document of another person was attached."))

    def get(
        self, request: HttpRequest, pk: int, delivery_id: int, attachment_id: int
    ) -> HttpResponse:
        worker, delivery, attachment = self.target(request, pk, delivery_id, attachment_id)
        return self.render(
            request, worker=worker, delivery=delivery, attachment=attachment, form=self.form()
        )

    def post(
        self, request: HttpRequest, pk: int, delivery_id: int, attachment_id: int
    ) -> HttpResponse:
        worker, delivery, attachment = self.target(request, pk, delivery_id, attachment_id)
        if not consume_once_token(request):
            return self.already_submitted(request, delivery_url(delivery))
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
                return redirect(delivery_url(delivery))
        return self.render(
            request, worker=worker, delivery=delivery, attachment=attachment, form=form
        )


BRIEFING_FORM_IDS = {SupportItem.PRE_GUIDANCE: "5-9", SupportItem.ORIENTATION: "5-8"}


class BriefingGenerateView(BriefingMixin, WorkerFormView):
    """事前ガイダンス・生活オリエンテーションの確認書（第5-9号・第5-8号）の生成（T32、ADR-0021）。

    基準日は記録の実施日。生成前の検査の不足の一覧を出し、警告があるときは確かめた印が要る。
    """

    required_action = Action.FORMS_GENERATE
    template_name = "support/briefing_generate.html"

    def page(
        self,
        request: HttpRequest,
        worker: ForeignWorker,
        briefing: Briefing,
        form: BriefingGenerateForm,
        result: form_checks.CheckResult | None,
    ) -> HttpResponse:
        return self.render(
            request,
            worker=worker,
            briefing=briefing,
            form_id=BRIEFING_FORM_IDS[SupportItem(briefing.record.kind)],
            form=form,
            result=result,
            back_url=reverse("workers:support_briefing", args=[worker.pk, briefing.pk]),
        )

    def check(
        self, briefing: Briefing, form: BriefingGenerateForm
    ) -> form_checks.CheckResult | None:
        try:
            return form_generation.check(
                BRIEFING_FORM_IDS[SupportItem(briefing.record.kind)],
                target=briefing,
                data=form_data_briefing.build(briefing),
                basis_on=briefing.record.occurred_on,
            )
        except form_templates.FormTemplateError:
            form.add_error(None, NOT_LOADED)
            return None

    def get(self, request: HttpRequest, pk: int, briefing_id: int) -> HttpResponse:
        worker = self.worker(request, pk)
        briefing = self.open_briefing(worker, briefing_id)
        form = BriefingGenerateForm()
        return self.page(request, worker, briefing, form, self.check(briefing, form))

    def post(self, request: HttpRequest, pk: int, briefing_id: int) -> HttpResponse:
        worker = self.worker(request, pk)
        briefing = self.open_briefing(worker, briefing_id)
        form = BriefingGenerateForm(request.POST)
        form.is_valid()
        if not consume_once_token(request):
            return self.already_submitted(
                request, reverse("workers:support_briefing", args=[worker.pk, briefing.pk])
            )
        assert isinstance(request.user, User)
        try:
            document = form_generation.request(
                BRIEFING_FORM_IDS[SupportItem(briefing.record.kind)],
                target=briefing,
                data=form_data_briefing.build(briefing),
                basis_on=briefing.record.occurred_on,
                by=request.user,
                accept_warnings=bool(form.cleaned_data.get("accept_warnings")),
            )
        except form_generation.GenerationBlocked as blocked:
            return self.page(request, worker, briefing, form, blocked.result)
        except form_templates.FormTemplateError:
            form.add_error(None, NOT_LOADED)
            return self.page(request, worker, briefing, form, None)
        except form_generation.WarningsNotAccepted as pending:
            form.add_error("accept_warnings", _("Check the warnings before generating."))
            return self.page(request, worker, briefing, form, pending.result)
        messages.success(request, _("Requested the generation of the form."))
        return redirect("forms:document", pk=document.pk)


# --- 定期面談報告書（第5-5号・第5-6号。T35、ADR-0024）---------------------------------------

INTERVIEW_FORM_IDS = ("5-5", "5-6")


def merged_result(results: list[form_checks.CheckResult]) -> form_checks.CheckResult:
    """2 つの様式の検査の結果を 1 つにする（同じ不足は 1 つ）。"""
    blocking: list[form_checks.Finding] = []
    warnings: list[form_checks.Finding] = []
    seen: set[tuple[str, str, str]] = set()
    for result in results:
        for finding in result.findings:
            key = (finding.rule, finding.code, finding.message)
            if key in seen:
                continue
            seen.add(key)
            (blocking if finding.severity == form_checks.BLOCK else warnings).append(finding)
    return form_checks.CheckResult(blocking, warnings)


class InterviewGenerateView(WorkerFormView):
    """定期面談の組から報告書（本人用 第5-5号・監督者用 第5-6号）をまとめて生成する（T35-D12）。

    基準日は面談の日（組の起算日）。作成年月日は生成した日。署名欄は無い。
    """

    required_action = Action.FORMS_GENERATE
    template_name = "support/interview_generate.html"

    def interview(self, worker: ForeignWorker, interview_id: int) -> PeriodicInterview:
        return get_object_or_404(
            support_interviews.interviews_of(worker), pk=interview_id, voided_at__isnull=True
        )

    def data(self, interview: PeriodicInterview, form_id: str) -> dict[str, Any]:
        return form_data_interview.build(interview, form_id, made_on=timezone.localdate())

    def check(
        self, interview: PeriodicInterview, form: BriefingGenerateForm
    ) -> form_checks.CheckResult | None:
        try:
            return merged_result(
                [
                    form_generation.check(
                        form_id,
                        target=interview,
                        data=self.data(interview, form_id),
                        basis_on=interview.basis_on,
                    )
                    for form_id in INTERVIEW_FORM_IDS
                ]
            )
        except form_templates.FormTemplateError:
            form.add_error(None, NOT_LOADED)
            return None

    def page(
        self,
        request: HttpRequest,
        worker: ForeignWorker,
        interview: PeriodicInterview,
        form: BriefingGenerateForm,
        result: form_checks.CheckResult | None,
    ) -> HttpResponse:
        return self.render(
            request,
            worker=worker,
            interview=interview,
            form=form,
            result=result,
            back_url=interview_url(interview),
        )

    def get(self, request: HttpRequest, pk: int, interview_id: int) -> HttpResponse:
        worker = self.worker(request, pk)
        interview = self.interview(worker, interview_id)
        form = BriefingGenerateForm()
        return self.page(request, worker, interview, form, self.check(interview, form))

    def post(self, request: HttpRequest, pk: int, interview_id: int) -> HttpResponse:
        worker = self.worker(request, pk)
        interview = self.interview(worker, interview_id)
        form = BriefingGenerateForm(request.POST)
        form.is_valid()
        if not consume_once_token(request):
            return self.already_submitted(request, interview_url(interview))
        result = self.check(interview, form)
        if result is None:
            return self.page(request, worker, interview, form, None)
        if result.blocking:
            return self.page(request, worker, interview, form, result)
        accepted = bool(form.cleaned_data.get("accept_warnings"))
        if result.warnings and not accepted:
            form.add_error("accept_warnings", _("Check the warnings before generating."))
            return self.page(request, worker, interview, form, result)
        assert isinstance(request.user, User)
        with transaction.atomic():
            for form_id in INTERVIEW_FORM_IDS:
                form_generation.request(
                    form_id,
                    target=interview,
                    data=self.data(interview, form_id),
                    basis_on=interview.basis_on,
                    by=request.user,
                    accept_warnings=True,
                )
        messages.success(
            request, _("Requested the generation of the interview reports (forms 5-5 and 5-6).")
        )
        return redirect("workers:documents", pk=worker.pk)
