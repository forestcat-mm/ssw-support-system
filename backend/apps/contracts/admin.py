from datetime import date
from typing import Any

from django import forms
from django.contrib import admin
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import FieldDoesNotExist
from django.forms.models import model_to_dict
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.utils.safestring import SafeString
from django.utils.translation import gettext_lazy as _

from apps.core.admin import TRACKED_READONLY_FIELDS, AttachmentInline, TrackedModelAdmin
from apps.core.admin_filters import DateRangeFilter
from apps.core.models import Action, AttachmentKind, User
from apps.hosts.models import HostOrganization
from apps.workers.models import ForeignWorker
from services import authorization, contracts

from .models import (
    EmploymentContract,
    EmploymentContractAllowance,
    EmploymentContractDeduction,
    EmploymentContractShift,
    EmploymentContractVersion,
)

VERSION_URL = "admin:contracts_employmentcontractversion_add"
DETAIL_ROW_COPY_EXCLUDE = ("id", "version", "created_at", "updated_at", "created_by", "updated_by")


def _actor(request: HttpRequest) -> User | None:
    return request.user if isinstance(request.user, User) else None


def _today() -> date:
    return timezone.localdate()


# --- 契約の見出し ------------------------------------------------------------------


class StatusFilter(admin.SimpleListFilter):
    title = _("status")
    parameter_name = "status"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, Any]]:
        return list(contracts.ContractStatus.choices)

    def queryset(self, request: HttpRequest, queryset: Any) -> Any:
        value = self.value()
        if value is None or value not in contracts.ContractStatus.values:
            return queryset
        return queryset.filter(contracts.status_q(contracts.ContractStatus(value), _today()))


class VersionListInline(admin.TabularInline[EmploymentContractVersion, EmploymentContract]):
    """版の一覧（読み取り専用。版の追加は「新しい版を追加」から）。"""

    model = EmploymentContractVersion
    extra = 0
    can_delete = False
    fields = ("version", "changed_on", "change_reason", "site", "wage_type", "base_wage")
    readonly_fields = fields
    show_change_link = True

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


class ContractForm(forms.ModelForm[EmploymentContract]):
    class Meta:
        model = EmploymentContract
        fields = ("terminated_on", "termination_reason", "termination_note")

    def __init__(self, *args: Any, request: HttpRequest | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.request = request

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        contract = self.instance
        terminated_on = cleaned.get("terminated_on")
        try:
            if terminated_on is None and contract.terminated_on is not None:
                user = self.request.user if self.request is not None else AnonymousUser()
                if not authorization.has_action(user, Action.CONTRACTS_REOPEN):
                    raise forms.ValidationError(_("Only administrators can cancel a termination."))
                contracts.validate_no_overlap(
                    contract.worker_id,
                    contract.current_starts_on,
                    contract.current_ends_on,
                    exclude_contract_id=contract.pk,
                )
            contracts.validate_termination(
                terminated_on,
                cleaned.get("termination_reason") or "",
                cleaned.get("termination_note") or "",
                contract.current_starts_on,
            )
        except forms.ValidationError as error:
            self.add_error(None, error)
        return cleaned


@admin.register(EmploymentContract)
class EmploymentContractAdmin(TrackedModelAdmin[EmploymentContract]):
    """雇用契約の見出し。作成は版の追加画面から（1 版目と一緒に作る）。削除はしない。"""

    form = ContractForm
    list_display = (
        "worker",
        "host_organization",
        "current_site",
        "current_starts_on",
        "current_ends_on",
        "current_wage",
        "status",
        "terminated_on",
    )
    list_filter = (StatusFilter, "host_organization", ("current_starts_on", DateRangeFilter))
    search_fields = ("worker__name_romaji", "worker__name_kanji", "host_organization__name")
    list_select_related = ("worker", "host_organization")
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "worker",
                    "host_organization",
                    "current_starts_on",
                    "current_ends_on",
                    "status",
                    "add_version_link",
                )
            },
        ),
        (_("Termination"), {"fields": ("terminated_on", "termination_reason", "termination_note")}),
        (_("Record"), {"fields": TRACKED_READONLY_FIELDS}),
    )
    readonly_fields = (
        "worker",
        "host_organization",
        "current_starts_on",
        "current_ends_on",
        "status",
        "add_version_link",
        *TRACKED_READONLY_FIELDS,
    )
    inlines = [VersionListInline]

    def get_form(
        self, request: HttpRequest, obj: Any = None, change: bool = False, **kwargs: Any
    ) -> Any:
        form_class = super().get_form(request, obj, change, **kwargs)

        class RequestBoundForm(form_class):  # type: ignore[misc, valid-type]
            def __init__(self, *args: Any, **inner: Any) -> None:
                super().__init__(*args, request=request, **inner)

        return RequestBoundForm

    def add_view(
        self, request: HttpRequest, form_url: str = "", extra_context: Any = None
    ) -> HttpResponse:
        # 新しい契約は 1 版目と一緒に、版の追加画面で作る
        return HttpResponseRedirect(reverse(VERSION_URL))

    @admin.display(description=_("status"))
    def status(self, obj: EmploymentContract) -> str:
        return str(contracts.status_of(obj, _today()).label)

    @admin.display(description=_("host site"))
    def current_site(self, obj: EmploymentContract) -> Any:
        latest = contracts.latest_version(obj)
        return latest.site if latest else "-"

    @admin.display(description=_("base wage (yen)"))
    def current_wage(self, obj: EmploymentContract) -> str:
        latest = contracts.latest_version(obj)
        return f"{latest.get_wage_type_display()} {latest.base_wage:,}" if latest else "-"

    @admin.display(description=_("new version"))
    def add_version_link(self, obj: EmploymentContract) -> SafeString:
        url = f"{reverse(VERSION_URL)}?contract={obj.pk}"
        return format_html('<a class="button" href="{}">{}</a>', url, _("Add a new version"))

    def save_model(
        self, request: HttpRequest, obj: EmploymentContract, form: Any, change: bool
    ) -> None:
        stored = EmploymentContract.objects.get(pk=obj.pk)
        actor = _actor(request)
        if obj.terminated_on is not None and stored.terminated_on is None:
            contracts.terminate(
                stored,
                on=obj.terminated_on,
                reason=obj.termination_reason,
                note=obj.termination_note,
                by=actor,
            )
        elif obj.terminated_on is None and stored.terminated_on is not None:
            contracts.cancel_termination(stored, by=actor)
        else:
            super().save_model(request, obj, form, change)

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


# --- 版 --------------------------------------------------------------------------


class ImmutableDetailInline(admin.TabularInline[Any, EmploymentContractVersion]):
    """版の明細。版を作るときだけ入力でき、既存の版では読み取り専用。"""

    extra = 0
    can_delete = False

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return obj is None and super().has_add_permission(request, obj)

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return obj is None and super().has_change_permission(request, obj)

    def get_extra(self, request: HttpRequest, obj: Any = None, **kwargs: Any) -> int:
        if obj is not None:
            return 0
        return len(_prefill_rows(request, self.model))


class ShiftInline(ImmutableDetailInline):
    model = EmploymentContractShift
    fields = ("start_time", "end_time", "applies_to", "daily_working_minutes")


class AllowanceInline(ImmutableDetailInline):
    model = EmploymentContractAllowance
    fields = ("name", "amount", "calculation_method", "fixed_overtime_hours")


class DeductionInline(ImmutableDetailInline):
    model = EmploymentContractDeduction
    fields = ("kind", "name", "amount")


class VersionAttachmentInline(AttachmentInline):
    """同等報酬の根拠資料・変形労働時間制の資料。既存の版にも後から追加できる（版の内容は変えない）。"""

    kinds = (AttachmentKind.EQUAL_PAY_EVIDENCE, AttachmentKind.VARIABLE_HOURS_DOCUMENTS)


class VersionForm(forms.ModelForm[EmploymentContractVersion]):
    """版の追加。contract を空にすると、外国人・受入れ機関を指定して新しい契約を作る。"""

    contract_choice = forms.ModelChoiceField(
        label=_("employment contract"),
        queryset=EmploymentContract.objects.select_related("worker", "host_organization"),
        required=False,
        help_text=_("Leave blank to create a new contract with version 1."),
    )
    new_worker = forms.ModelChoiceField(
        label=_("foreign worker"), queryset=ForeignWorker.objects.all(), required=False
    )
    new_host_organization = forms.ModelChoiceField(
        label=_("host organization"), queryset=HostOrganization.objects.all(), required=False
    )

    class Meta:
        model = EmploymentContractVersion
        exclude = ("contract",)  # noqa: DJ006  版の項目が多いため、除外する列だけを書く

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        if self.instance.pk is not None:
            return cleaned  # 既存の版は変更しない（根拠資料の添付だけを受け付ける）
        contract: EmploymentContract | None = cleaned.get("contract_choice")
        host = contract.host_organization if contract else cleaned.get("new_host_organization")
        worker = contract.worker if contract else cleaned.get("new_worker")
        if contract is None and (worker is None or host is None):
            self.add_error(
                None, _("Select an existing contract, or a foreign worker and a host organization.")
            )
            return cleaned
        checks = [
            ("site", lambda: contracts.validate_site_belongs_to_host(host, cleaned.get("site"))),
            (
                "categories",
                lambda: contracts.validate_categories_of_site(
                    cleaned.get("site"), cleaned.get("categories") or []
                ),
            ),
            (
                "change_reason",
                lambda: contracts.validate_change(contract, cleaned.get("change_reason") or ""),
            ),
            (
                "starts_on",
                lambda: contracts.validate_no_overlap(
                    getattr(worker, "pk", None),
                    cleaned.get("starts_on"),
                    cleaned.get("ends_on"),
                    contract.terminated_on if contract else None,
                    exclude_contract_id=contract.pk if contract else None,
                ),
            ),
        ]
        for field, check in checks:
            try:
                check()
            except forms.ValidationError as error:
                self.add_error(field, error)
        return cleaned


SECTIONS: tuple[tuple[Any, tuple[str, ...]], ...] = (
    (_("Contract"), ("contract_choice", "new_worker", "new_host_organization")),
    (_("Change"), ("change_reason", "changed_on", "signed_on")),
    (
        _("I. Contract period"),
        (
            "starts_on",
            "ends_on",
            "expected_entry_on",
            "renewal",
            "renewal_criteria",
            "renewal_criteria_other",
            "has_renewal_limit",
            "renewal_limit_count",
            "renewal_limit_years",
        ),
    ),
    (_("II. Workplace"), ("site", "workplace_change_scope")),
    (_("III. Duties"), ("categories", "duties", "duty_change_scope")),
    (
        _("IV. Working hours"),
        (
            "start_time",
            "end_time",
            "daily_working_minutes",
            "variable_hours_unit",
            "break_minutes",
            "weekly_working_minutes",
            "monthly_working_minutes",
            "annual_working_minutes",
            "weekly_working_days",
            "monthly_working_days",
            "annual_working_days",
            "has_overtime",
        ),
    ),
    (
        _("V. Holidays"),
        (
            "regular_holiday_weekdays",
            "national_holidays_off",
            "regular_holidays_other",
            "annual_holidays",
            "irregular_holidays",
        ),
    ),
    (
        _("VI. Leave"),
        (
            "paid_leave_days",
            "has_early_paid_leave",
            "early_paid_leave_months",
            "early_paid_leave_days",
            "other_paid_leave",
            "other_unpaid_leave",
            "temporary_return_leave_note",
        ),
    ),
    (
        _("VII. Wages"),
        (
            "wage_type",
            "base_wage",
            "hourly_equivalent",
            "monthly_equivalent",
            "overtime_rate_within_60h",
            "overtime_rate_over_60h",
            "overtime_rate_scheduled",
            "holiday_rate_statutory",
            "holiday_rate_non_statutory",
            "night_rate",
            "wage_closing_day",
            "wage_payment_day",
            "payment_method",
            "has_deduction_agreement",
            "has_raise",
            "raise_note",
            "has_bonus",
            "bonus_note",
            "has_retirement_allowance",
            "retirement_allowance_note",
            "leave_allowance_rate",
        ),
    ),
    (_("VIII. Resignation"), ("resignation_notice_days",)),
    (
        _("IX. Others"),
        (
            "insurances",
            "insurance_other",
            "hiring_health_check_on",
            "first_periodic_health_check_on",
            "periodic_health_check_interval",
            "treatment_explanation_contact",
            "consultation_contact",
            "work_rules_location",
        ),
    ),
    (_("Equal pay"), ("comparable_japanese_note", "comparable_japanese_wage")),
)


def _model_fields(names: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        n for n in names if n not in ("contract_choice", "new_worker", "new_host_organization")
    )


def _prefill_contract(request: HttpRequest) -> EmploymentContract | None:
    contract_id = request.GET.get("contract")
    if not contract_id or not contract_id.isdigit():
        return None
    return EmploymentContract.objects.filter(pk=int(contract_id)).first()


def _prefill_rows(request: HttpRequest, model: type[Any]) -> list[dict[str, Any]]:
    """最新の版の明細行を、新しい版の初期値として返す。"""
    contract = _prefill_contract(request)
    latest = contracts.latest_version(contract) if contract else None
    if latest is None:
        return []
    rows = model.objects.filter(version=latest).order_by("pk")
    return [model_to_dict(row, exclude=DETAIL_ROW_COPY_EXCLUDE) for row in rows]


@admin.register(EmploymentContractVersion)
class EmploymentContractVersionAdmin(TrackedModelAdmin[EmploymentContractVersion]):
    """雇用契約の版。追加だけでき、既存の版は読み取り専用（根拠資料の添付だけ追加できる）。"""

    form = VersionForm
    list_display = ("contract", "version", "changed_on", "site", "wage_type", "base_wage")
    list_filter = ("wage_type", "contract__host_organization")
    search_fields = ("contract__worker__name_romaji", "contract__worker__name_kanji")
    filter_horizontal = ("categories",)
    inlines = [ShiftInline, AllowanceInline, DeductionInline, VersionAttachmentInline]

    def get_fieldsets(self, request: HttpRequest, obj: Any = None) -> Any:
        if obj is None:
            return [(title, {"fields": fields}) for title, fields in SECTIONS]
        head = (
            None,
            {
                "fields": (
                    "contract",
                    "version",
                    "changes_from_previous",
                    "monthly_total",
                    "take_home",
                )
            },
        )
        body = [(title, {"fields": _model_fields(fields)}) for title, fields in SECTIONS[1:]]
        return [head, *body, (_("Record"), {"fields": TRACKED_READONLY_FIELDS})]

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> Any:
        if obj is None:
            return ()
        names = [n for _, fields in SECTIONS[1:] for n in _model_fields(fields)]
        extra = ("contract", "version", "changes_from_previous", "monthly_total", "take_home")
        return (*extra, *names, *TRACKED_READONLY_FIELDS)

    def get_changeform_initial_data(self, request: HttpRequest) -> dict[str, Any]:
        contract = _prefill_contract(request)
        latest = contracts.latest_version(contract) if contract else None
        if contract is None or latest is None:
            return super().get_changeform_initial_data(request)
        initial = model_to_dict(
            latest,
            exclude=("id", "contract", "version", "change_reason", "changed_on", "signed_on"),
        )
        initial["categories"] = [c.pk for c in latest.categories.all()]
        initial["contract_choice"] = contract.pk
        return initial

    def get_formset_kwargs(
        self, request: HttpRequest, obj: Any, inline: Any, prefix: str
    ) -> dict[str, Any]:
        kwargs = super().get_formset_kwargs(request, obj, inline, prefix)
        # 追加画面では obj に保存前の空の版が渡される（None ではない）
        adding = obj is None or obj.pk is None
        if adding and request.method == "GET" and isinstance(inline, ImmutableDetailInline):
            kwargs["initial"] = _prefill_rows(request, inline.model)
        return kwargs

    @admin.display(description=_("changes from the previous version"))
    def changes_from_previous(self, obj: EmploymentContractVersion) -> SafeString | str:
        previous = contracts.previous_version(obj)
        if previous is None:
            return "-"
        changes = contracts.diff(previous, obj)
        if not changes:
            return "-"
        rows = format_html_join(
            "",
            "<tr><td>{}</td><td>{}</td><td>{}</td></tr>",
            ((_label(name), before, after) for name, (before, after) in changes.items()),
        )
        return format_html(
            "<table><thead><tr><th>{}</th><th>{}</th><th>{}</th></tr></thead><tbody>{}</tbody></table>",
            _("field"),
            _("before"),
            _("after"),
            rows,
        )

    @admin.display(description=_("approximate monthly payment (yen)"))
    def monthly_total(self, obj: EmploymentContractVersion) -> str:
        total = contracts.monthly_total(obj)
        return f"{total:,}" if total is not None else "-"

    @admin.display(description=_("approximate take-home pay (yen)"))
    def take_home(self, obj: EmploymentContractVersion) -> str:
        amount = contracts.take_home(obj)
        return f"{amount:,}" if amount is not None else "-"

    def save_model(
        self, request: HttpRequest, obj: EmploymentContractVersion, form: Any, change: bool
    ) -> None:
        if change:
            return  # 既存の版は変更しない（根拠資料の添付だけが保存される）
        contracts.add_version(
            obj,
            contract=form.cleaned_data.get("contract_choice"),
            worker=form.cleaned_data.get("new_worker"),
            host_organization=form.cleaned_data.get("new_host_organization"),
            by=_actor(request),
        )

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


DETAIL_LABELS = {
    "shifts": _("shifts"),
    "allowances": _("allowances"),
    "deductions": _("deductions"),
}


def _label(name: str) -> str:
    """差分の項目名（明細は明細の名前、それ以外は版の列の表示名）。"""
    if name in DETAIL_LABELS:
        return str(DETAIL_LABELS[name])
    try:
        field = EmploymentContractVersion._meta.get_field(name)
    except FieldDoesNotExist:
        return name
    return str(getattr(field, "verbose_name", name))
