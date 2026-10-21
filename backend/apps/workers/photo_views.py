"""申請用の写真の画面（U4。ADR-0047）。

登録・取消は workers.manage（無効化していない外国人だけ）。画像の取り出しと
ダウンロードは、その外国人を見られる人（authorization.scope）だけ。ファイルの URL は
作らない（PrivateFileField）。
"""

from __future__ import annotations

from typing import Any

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views import View

from apps.core.models import Action, User
from apps.core.permissions import ActionRequiredMixin
from apps.core.web_forms import StyledFormMixin, consume_once_token
from apps.support.record_forms import ReasonForm
from apps.workers.models import ForeignWorker, WorkerPhoto
from apps.workers.views import WorkerFormView
from services import authorization, worker_photos


class PhotoForm(StyledFormMixin, forms.Form):
    upload = forms.FileField(
        label=gettext_lazy("photo file"),
        help_text=gettext_lazy("JPEG or PNG, up to 10 MB."),
        widget=forms.ClearableFileInput(attrs={"accept": "image/jpeg,image/png"}),
    )
    taken_on = forms.DateField(
        label=gettext_lazy("taken on"),
        widget=forms.DateInput,
        help_text=gettext_lazy(
            "The photo for an application must be taken within 6 months before the application."
        ),
    )
    # 切り抜きの枠（元の画像の画素。画面の枠で決める。無ければ真ん中の 3:4）
    crop_left = forms.IntegerField(required=False, min_value=0, widget=forms.HiddenInput)
    crop_top = forms.IntegerField(required=False, min_value=0, widget=forms.HiddenInput)
    crop_width = forms.IntegerField(required=False, min_value=0, widget=forms.HiddenInput)
    crop_height = forms.IntegerField(required=False, min_value=0, widget=forms.HiddenInput)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()

    def box(self) -> worker_photos.Box | None:
        values = [
            self.cleaned_data.get(name)
            for name in ("crop_left", "crop_top", "crop_width", "crop_height")
        ]
        if any(value is None for value in values) or not values[2]:
            return None
        left, top, width, height = (int(value) for value in values)  # type: ignore[arg-type]
        return worker_photos.Box(left, top, width, height)


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)
    return request.user


def detail_url(worker: ForeignWorker) -> str:
    return reverse("workers:detail", args=[worker.pk])


class PhotoCreateView(WorkerFormView):
    """写真を登録する（前の写真は履歴に残る）。"""

    template_name = "workers/photo_form.html"

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        worker = self.worker(request, pk)
        return self.render(
            request, worker=worker, form=PhotoForm(), current=worker_photos.latest(worker)
        )

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        worker = self.worker(request, pk)
        if not consume_once_token(request):
            return self.already_submitted(request, detail_url(worker))
        form = PhotoForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                worker_photos.register(
                    worker,
                    form.cleaned_data["upload"],
                    taken_on=form.cleaned_data["taken_on"],
                    box=form.box(),
                    by=_actor(request),
                )
            except ValidationError as error:
                for key, errors in error.message_dict.items():
                    form.add_error(key if key in form.fields else None, errors)
            else:
                messages.success(request, _("Registered the application photo."))
                return redirect(detail_url(worker))
        return self.render(request, worker=worker, form=form, current=worker_photos.latest(worker))


class PhotoVoidView(WorkerFormView):
    """写真の取消（理由は必須。ファイルは消さずに履歴に残す）。"""

    template_name = "workers/photo_void.html"

    def target(
        self, request: HttpRequest, pk: int, photo_id: int
    ) -> tuple[ForeignWorker, WorkerPhoto]:
        worker = self.worker(request, pk)
        photo = get_object_or_404(WorkerPhoto, pk=photo_id, worker=worker, voided_at__isnull=True)
        return worker, photo

    def get(self, request: HttpRequest, pk: int, photo_id: int) -> HttpResponse:
        worker, photo = self.target(request, pk, photo_id)
        return self.render(request, worker=worker, photo=photo, form=ReasonForm())

    def post(self, request: HttpRequest, pk: int, photo_id: int) -> HttpResponse:
        worker, photo = self.target(request, pk, photo_id)
        history_url = reverse("workers:history", args=[pk]) + "#photos"
        if not consume_once_token(request):
            return self.already_submitted(request, history_url)
        form = ReasonForm(request.POST)
        if form.is_valid():
            worker_photos.void(photo, reason=form.cleaned_data["reason"], by=_actor(request))
            messages.success(request, _("Voided the record."))
            return redirect(history_url)
        return self.render(request, worker=worker, photo=photo, form=form)


class PhotoFileMixin(ActionRequiredMixin):
    required_action = Action.WORKERS_VIEW

    def photo(self, request: HttpRequest, pk: int, photo_id: int) -> WorkerPhoto:
        queryset = authorization.scope(request.user, ForeignWorker.objects.all())
        worker = get_object_or_404(queryset, pk=pk)
        return get_object_or_404(
            WorkerPhoto.objects.select_related("worker"), pk=photo_id, worker=worker
        )


class PhotoImageView(PhotoFileMixin, View):
    """画面に出す写真（切り抜いた画像）。監査ログは残さない（画面の閲覧 1 件に含める。U4-D5）。"""

    def get(self, request: HttpRequest, pk: int, photo_id: int) -> HttpResponse:
        photo = self.photo(request, pk, photo_id)
        response = HttpResponse(worker_photos.image_bytes(photo), content_type="image/jpeg")
        response["Cache-Control"] = "private, max-age=300"
        response["X-Content-Type-Options"] = "nosniff"
        return response


class PhotoDownloadView(PhotoFileMixin, View):
    """オンライン申請に出す JPEG のダウンロード（監査ログに download）。"""

    def get(self, request: HttpRequest, pk: int, photo_id: int) -> HttpResponse:
        photo = self.photo(request, pk, photo_id)
        content = worker_photos.download_jpeg(photo, by=_actor(request))
        response = HttpResponse(content, content_type="image/jpeg")
        response["Content-Disposition"] = (
            f'attachment; filename="{worker_photos.jpeg_filename(photo)}"'
        )
        response["Cache-Control"] = "no-store"
        return response
