from typing import Any

from django import forms
from django.contrib import admin
from django.core.exceptions import ObjectDoesNotExist
from django.core.validators import MaxLengthValidator
from django.db.models import QuerySet
from django.forms import ModelForm
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _

from apps.core.admin import TRACKED_READONLY_FIELDS, TrackedModelAdmin
from apps.core.admin_filters import DateRangeFilter
from apps.core.models import User
from services import passport, residence_card, workers

from .models import (
    ForeignWorker,
    HistoryRecord,
    PassportRecord,
    ResidenceRecord,
    WorkerAddress,
    WorkerContact,
)

VOID_READONLY_FIELDS: tuple[str, ...] = ("voided_at", "voided_by", "void_reason")

# 番号は空白・ハイフン付きでも入力できるようにし、正規化した後に形式（桁数）を検査する
NUMBER_INPUT_MAX_LENGTH = 30


def _allow_formatted_number_input(field: forms.Field) -> None:
    assert isinstance(field, forms.CharField)
    field.max_length = NUMBER_INPUT_MAX_LENGTH
    field.validators = [v for v in field.validators if not isinstance(v, MaxLengthValidator)]
    field.widget.attrs["maxlength"] = str(NUMBER_INPUT_MAX_LENGTH)


class HistoryInlineForm(forms.ModelForm[Any]):
    """履歴行のフォーム。既存の行は「取り消し」とその理由だけを入力でき、ほかは変更できない。"""

    void = forms.BooleanField(label=_("void"), required=False)
    void_reason_input = forms.CharField(label=_("void reason"), required=False, max_length=200)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self.instance.pk is None:
            # admin はインラインの全行に同じ項目を求めるため、新しい行では隠して使わない
            for name in ("void", "void_reason_input"):
                self.fields[name].widget = forms.HiddenInput()
                self.fields[name].disabled = True
            return
        for name, field in self.fields.items():
            if name not in ("void", "void_reason_input") or self.instance.voided_at is not None:
                field.disabled = True

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        if self.instance.pk is not None and cleaned.get("void"):
            if not str(cleaned.get("void_reason_input", "")).strip():
                self.add_error("void_reason_input", _("Enter the reason for voiding."))
        return cleaned

    def wants_void(self) -> bool:
        return bool(self.instance.pk and self.cleaned_data.get("void"))


class ResidenceRecordForm(HistoryInlineForm):
    class Meta:
        model = ResidenceRecord
        fields = (
            "application_type",
            "status_of_residence",
            "card_number",
            "permitted_on",
            "expires_on",
        )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        _allow_formatted_number_input(self.fields["card_number"])

    def clean_card_number(self) -> str:
        value = residence_card.normalize(self.cleaned_data.get("card_number") or "")
        if self.instance.pk is None and value:
            residence_card.validate(value)
            workers.validate_number_not_taken(ResidenceRecord, value, self.instance.worker_id)
        return value

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean()
        if self.instance.pk is None:
            try:
                workers.validate_period(cleaned.get("permitted_on"), cleaned.get("expires_on"))
            except forms.ValidationError as error:
                self.add_error("expires_on", error)
        return cleaned


class PassportRecordForm(HistoryInlineForm):
    class Meta:
        model = PassportRecord
        fields = ("number", "issuing_country", "issued_on", "expires_on")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        _allow_formatted_number_input(self.fields["number"])

    def clean_number(self) -> str:
        value = passport.normalize(self.cleaned_data.get("number") or "")
        if self.instance.pk is None:
            passport.validate(value)
            workers.validate_number_not_taken(PassportRecord, value, self.instance.worker_id)
        return value

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean()
        if self.instance.pk is None:
            try:
                workers.validate_period(cleaned.get("issued_on"), cleaned.get("expires_on"))
            except forms.ValidationError as error:
                self.add_error("expires_on", error)
        return cleaned


class WorkerAddressForm(HistoryInlineForm):
    class Meta:
        model = WorkerAddress
        fields = ("effective_from", "postal_code", "address")


class WorkerContactForm(HistoryInlineForm):
    class Meta:
        model = WorkerContact
        fields = (
            "effective_from",
            "phone",
            "email",
            "emergency_name",
            "emergency_relationship",
            "emergency_phone",
            "home_country_address",
            "home_country_phone",
        )


class HistoryInline(admin.StackedInline[Any, ForeignWorker]):
    extra = 0
    can_delete = False
    readonly_fields = VOID_READONLY_FIELDS
    show_change_link = False


class ResidenceRecordInline(HistoryInline):
    model = ResidenceRecord
    form = ResidenceRecordForm


class PassportRecordInline(HistoryInline):
    model = PassportRecord
    form = PassportRecordForm


class WorkerAddressInline(HistoryInline):
    model = WorkerAddress
    form = WorkerAddressForm
    readonly_fields = (*VOID_READONLY_FIELDS, "confirmed_by", "confirmed_at")


class WorkerContactInline(HistoryInline):
    model = WorkerContact
    form = WorkerContactForm
    readonly_fields = (*VOID_READONLY_FIELDS, "confirmed_by", "confirmed_at")


@admin.register(ForeignWorker)
class ForeignWorkerAdmin(TrackedModelAdmin[ForeignWorker]):
    """外国人台帳。履歴（在留・旅券・住所・連絡先）は上書きせず行を足す。削除はしない。"""

    list_display = (
        "name_romaji",
        "name_kanji",
        "nationality",
        "status",
        "assigned_staff",
        "latest_status_of_residence",
        "latest_expires_on",
    )
    list_filter = (
        "status",
        "nationality",
        "assigned_staff",
        ("latest_residence__record__expires_on", DateRangeFilter),
    )
    # 氏名は部分一致。在留カード番号・旅券番号は完全一致（ブラインドインデックス、表記ゆれは吸収）
    search_fields = ("name_romaji", "name_kanji", "name_native")
    search_help_text = _("Name (partial match), or residence card / passport number (exact match).")
    list_select_related = ("nationality", "assigned_staff", "latest_residence__record")
    fieldsets = (
        (None, {"fields": ("name_romaji", "name_kanji", "name_native")}),
        (
            _("Profile"),
            {"fields": ("nationality", "birth_date", "sex", "support_language")},
        ),
        (_("Support"), {"fields": ("status", "assigned_staff")}),
        (
            _("Record"),
            {"fields": ("confirmed_by", "confirmed_at", *TRACKED_READONLY_FIELDS)},
        ),
    )
    readonly_fields = ("confirmed_by", "confirmed_at", *TRACKED_READONLY_FIELDS)
    inlines = [
        ResidenceRecordInline,
        PassportRecordInline,
        WorkerAddressInline,
        WorkerContactInline,
    ]

    @admin.display(description=_("status of residence"))
    def latest_status_of_residence(self, obj: ForeignWorker) -> str:
        record = _latest_residence(obj)
        return record.get_status_of_residence_display() if record else "-"

    @admin.display(
        description=_("period of stay expires on"),
        ordering="latest_residence__record__expires_on",
    )
    def latest_expires_on(self, obj: ForeignWorker) -> Any:
        record = _latest_residence(obj)
        return record.expires_on if record else None

    def get_search_results(
        self, request: HttpRequest, queryset: QuerySet[ForeignWorker], search_term: str
    ) -> tuple[QuerySet[ForeignWorker], bool]:
        results, may_have_duplicates = super().get_search_results(request, queryset, search_term)
        ids = workers.worker_ids_by_document_number(search_term)
        if ids:
            results = results | queryset.filter(pk__in=ids)
        return results, may_have_duplicates

    def has_delete_permission(self, request: HttpRequest, obj: ForeignWorker | None = None) -> bool:
        return False

    def save_model(
        self, request: HttpRequest, obj: ForeignWorker, form: ModelForm[ForeignWorker], change: bool
    ) -> None:
        workers.confirm(obj, by=_actor(request))
        super().save_model(request, obj, form, change)

    def save_formset(
        self, request: HttpRequest, form: ModelForm[ForeignWorker], formset: Any, change: bool
    ) -> None:
        actor = _actor(request)
        for inline_form in formset.forms:
            if isinstance(inline_form, HistoryInlineForm) and inline_form.wants_void():
                workers.void_record(
                    inline_form.instance,
                    by=actor,
                    reason=inline_form.cleaned_data["void_reason_input"],
                )
        for instance in formset.save(commit=False):
            if instance.pk is not None:
                continue  # 既存の履歴行は変更しない（取り消しは上で処理済み）
            assert isinstance(instance, HistoryRecord)
            instance.created_by = actor
            instance.updated_by = actor
            workers.confirm(instance, by=actor)
            instance.save()


def _latest_residence(obj: ForeignWorker) -> ResidenceRecord | None:
    try:
        return obj.latest_residence.record
    except ObjectDoesNotExist:
        return None


def _actor(request: HttpRequest) -> User | None:
    return request.user if isinstance(request.user, User) else None
