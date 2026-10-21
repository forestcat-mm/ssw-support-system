"""一括申請の画面のフォーム（T48。ADR-0039）。検査・生成は services/bulk_applications。"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import Staff
from apps.core.web_forms import StyledFormMixin
from apps.forms_engine.models import BulkApplication, BulkApplicationEntry
from apps.hosts.models import HostApplicantContact
from services import bulk_application_data, bulk_checks, immigration_codes

YES_NO = [("", "---------"), ("yes", _("yes")), ("no", _("no"))]


class BulkApplicationForm(StyledFormMixin, forms.ModelForm[BulkApplication]):
    class Meta:
        model = BulkApplication
        fields = ("procedure", "basis_on", "title", "intermediary")
        widgets = {"basis_on": forms.DateInput}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        intermediary = self.fields["intermediary"]
        assert isinstance(intermediary, forms.ModelChoiceField)
        intermediary.queryset = Staff.objects.filter(
            is_active=True, is_online_application_user=True
        ).order_by("name_kana", "name")
        intermediary.help_text = _(
            "Staff members marked as users of the online residence application system "
            "(settings, staff)."
        )
        if not self.is_bound and not self.initial.get("basis_on"):
            self.initial["basis_on"] = timezone.localdate()
        self.style_fields()


@dataclass(frozen=True)
class InputField:
    name: str
    label: Any
    kind: str  # yes_no / text / code / date / textarea
    procedures: tuple[str, ...] = ("coe", "extension", "change")
    key: str = ""  # 欄の対応のキー（kind=code のとき業務コードを引く）
    max_length: int | None = None
    help_text: Any = ""
    prefix: str = ""  # 選択肢を表示名の頭で絞る（希望する在留期間）


ALL = ("coe", "extension", "change")
RENEWALS = ("extension", "change")

INPUT_FIELDS: tuple[InputField, ...] = (
    InputField("spouse", _("spouse"), "yes_no"),
    InputField("occupation", _("occupation"), "text", max_length=40),
    # 認定
    # 日本における連絡先（入国前なので台帳の住所ではなく申請ごとに入れる。ADR-0039 決定 17）
    InputField(
        "contact_postal",
        _("contact in Japan: postal code"),
        "text",
        ("coe",),
        max_length=8,
        help_text=_("Example: 272-0023"),
    ),
    InputField(
        "contact_address",
        _("contact in Japan: address"),
        "text",
        ("coe",),
        max_length=255,
        help_text=_(
            "Write from the prefecture. It is split into the prefecture, the municipality "
            "and the rest of the address of the tool."
        ),
    ),
    InputField(
        "contact_phone",
        _("contact in Japan: phone number"),
        "text",
        ("coe",),
        max_length=20,
        help_text=_("A number starting with 070, 080 or 090 goes to the mobile phone column."),
    ),
    InputField(
        "entry_month",
        _("planned month of entry"),
        "text",
        ("coe",),
        max_length=40,
        help_text=_("Example: 2027年4月"),
    ),
    InputField("port", _("planned port of entry"), "code", ("coe",), key="a.port"),
    InputField("port_other", _("port of entry (other)"), "text", ("coe",), max_length=40),
    InputField(
        "stay_years", _("planned length of stay (years)"), "code", ("coe",), key="a.stay_years"
    ),
    InputField(
        "stay_months", _("planned length of stay (months)"), "code", ("coe",), key="a.stay_months"
    ),
    InputField("companion", _("accompanying persons"), "yes_no", ("coe",)),
    InputField(
        "visa_place", _("planned place of visa application"), "text", ("coe",), max_length=40
    ),
    InputField("entry_history", _("past entries into Japan"), "yes_no", ("coe",)),
    InputField("entry_count", _("number of past entries"), "code", ("coe",), key="d.entry_count"),
    InputField("last_entry_from", _("latest entry (from)"), "date", ("coe",)),
    InputField("last_entry_to", _("latest entry (to)"), "date", ("coe",)),
    InputField(
        "coe_history", _("past applications for a certificate of eligibility"), "yes_no", ("coe",)
    ),
    InputField("coe_count", _("number of past applications"), "code", ("coe",), key="d.coe_count"),
    InputField("coe_refused", _("of which not issued"), "code", ("coe",), key="d.coe_refused"),
    InputField("deport", _("departure by deportation or departure order"), "yes_no", ("coe",)),
    InputField("deport_count", _("number of deportations"), "code", ("coe",), key="d.deport_count"),
    InputField("deport_last", _("latest deportation"), "date", ("coe",)),
    # 更新・変更
    InputField(
        "desired_period",
        _("desired period of stay"),
        "code",
        RENEWALS,
        key="a.desired_period",
        prefix="特定技能１号",
    ),
    InputField("reason", _("reason for the application"), "textarea", RENEWALS, max_length=600),
    InputField(
        "ed_alpha",
        _("ED number (letters)"),
        "text",
        RENEWALS,
        max_length=4,
        help_text=_("Only when the worker has no residence card."),
    ),
    InputField("ed_number", _("ED number (digits)"), "text", RENEWALS, max_length=7),
    # 受領・通知
    InputField("receipt", _("how to receive"), "code", ALL, key="a.receipt"),
    InputField(
        "receipt_residence",
        _("residence of the applicant (counter)"),
        "code",
        RENEWALS,
        key="a.receipt_residence",
    ),
    InputField("receipt_office", _("receiving office"), "code", RENEWALS, key="a.receipt_office"),
    InputField("notice_email", _("email for notices"), "text", max_length=60),
    InputField("free", _("free entry"), "textarea", max_length=300),
    # 雇用契約（ブロック A の確かめてほしい点。T48-D2）
    InputField("main_occupation", _("main job category"), "code", key="c.main_occupation"),
    InputField("other_occupation1", _("other job category 1"), "code", key="c.other_occupation1"),
    InputField("other_occupation2", _("other job category 2"), "code", key="c.other_occupation2"),
    InputField("other_occupation3", _("other job category 3"), "code", key="c.other_occupation3"),
    InputField("hours_equal", _("working hours equal to regular workers"), "yes_no"),
    InputField("pay_equal", _("pay equal to or higher than Japanese workers"), "yes_no"),
    InputField("discrimination", _("different treatment because of being a foreigner"), "yes_no"),
    InputField(
        "discrimination_detail", _("details of the different treatment"), "text", max_length=60
    ),
    InputField("home_leave", _("paid leave for a temporary return home"), "yes_no"),
    InputField("return_travel", _("bearing the travel cost home when the worker cannot"), "yes_no"),
    InputField("health", _("measures to understand health and living conditions"), "yes_no"),
    InputField("plan_appropriate", _("the support contributes to an appropriate stay"), "yes_no"),
    # 保証金と費用（事前ガイダンスの記録が無いとき）
    InputField("deposit", _("contract on a deposit or penalty"), "yes_no"),
    InputField(
        "deposit_collector", _("organization collecting the deposit"), "text", max_length=60
    ),
    InputField("deposit_amount", _("deposit amount or property managed"), "text", max_length=40),
    InputField("foreign_fee", _("agreed fees paid to organizations abroad"), "yes_no"),
    InputField("foreign_fee_org", _("organization abroad"), "text", max_length=60),
    InputField("foreign_fee_amount", _("amount paid"), "text", max_length=40),
)


def fields_for(procedure: str) -> list[InputField]:
    return [field for field in INPUT_FIELDS if procedure in field.procedures]


class EntryForm(StyledFormMixin, forms.Form):
    """申請ごとの入力（犯罪の申告は見られる人だけに出す。ADR-0038 決定 6）。"""

    agent = forms.ModelChoiceField(
        label=_("agent (applicant contact of the host organization)"),
        queryset=HostApplicantContact.objects.none(),
        required=False,
    )
    crime = forms.ChoiceField(label=_("disposition for a crime"), choices=YES_NO, required=False)
    crime_detail = forms.CharField(
        label=_("details of the disposition"), max_length=40, required=False
    )

    def __init__(
        self,
        *args: Any,
        entry: BulkApplicationEntry,
        definition: dict[str, Any],
        show_sensitive: bool,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.entry = entry
        application = entry.application
        self.procedure = bulk_application_data.PROCEDURE_KEYS[application.procedure]  # type: ignore[index]
        codes = {item["key"]: item.get("code", "") for item in definition["fields"]}
        contract = bulk_application_data.contract_of(entry.worker, application.basis_on)
        agent = self.fields["agent"]
        assert isinstance(agent, forms.ModelChoiceField)
        agent.queryset = (
            HostApplicantContact.objects.filter(
                host_organization=contract.host_organization, is_active=True
            )
            if contract
            else HostApplicantContact.objects.none()
        )
        self.input_names = []
        for spec in fields_for(self.procedure):
            field = self._field(spec, codes)
            if field is None:
                continue
            self.fields[spec.name] = field
            self.input_names.append(spec.name)
            self.initial.setdefault(spec.name, (entry.inputs or {}).get(spec.name, ""))
        self.initial.setdefault("agent", entry.agent_id)
        self.show_sensitive = show_sensitive
        if show_sensitive:
            current = bulk_application_data.sensitive_of(entry)
            self.initial.setdefault("crime", current.get("crime", ""))
            self.initial.setdefault("crime_detail", current.get("crime_detail", ""))
        else:
            del self.fields["crime"]
            del self.fields["crime_detail"]
        self.style_fields()

    def _field(self, spec: InputField, codes: dict[str, str]) -> forms.Field | None:
        if spec.kind == "yes_no":
            return forms.ChoiceField(label=spec.label, choices=YES_NO, required=False)
        if spec.kind == "code":
            code = codes.get(spec.key)
            if not code:
                return None
            rows = immigration_codes.procedure_table(code, self.procedure)
            if spec.prefix:
                # 表示名の頭で絞る（更新の表は「特定技能１号（１年）」。変更の表は「１年」で、
                # 絞ると空になるので絞らない）
                narrowed = [
                    (value, label) for value, label in rows if label.startswith(spec.prefix)
                ]
                rows = narrowed or rows
            return forms.ChoiceField(
                label=spec.label,
                choices=[("", "---------")]
                + [(value, immigration_codes.japanese(label)) for value, label in rows],
                required=False,
                help_text=spec.help_text,
            )
        if spec.kind == "date":
            return forms.DateField(
                label=spec.label, required=False, widget=forms.DateInput, help_text=spec.help_text
            )
        widget = forms.Textarea(attrs={"rows": 3}) if spec.kind == "textarea" else None
        return forms.CharField(
            label=spec.label,
            max_length=spec.max_length,
            required=False,
            widget=widget,
            help_text=spec.help_text,
        )

    def input_fields(self) -> list[Any]:
        return [self[name] for name in self.input_names]

    def inputs(self) -> dict[str, str]:
        result = {}
        for name in self.input_names:
            value = self.cleaned_data.get(name)
            if isinstance(value, datetime.date):
                value = value.strftime("%Y%m%d")
            result[name] = value or ""
        return result

    def sensitive(self) -> dict[str, str] | None:
        if not self.show_sensitive:
            return None
        return {
            "crime": self.cleaned_data.get("crime") or "",
            "crime_detail": self.cleaned_data.get("crime_detail") or "",
        }

    def clean_ed_alpha(self) -> str:
        value = (self.cleaned_data.get("ed_alpha") or "").upper()
        if value and not bulk_checks.char_type_ok(value, ["半角英字"]):
            raise forms.ValidationError(_("Enter letters only."))
        return value


class AddWorkersForm(forms.Form):
    workers = forms.MultipleChoiceField(
        label=_("foreign workers"), widget=forms.CheckboxSelectMultiple, required=True
    )

    def __init__(self, *args: Any, choices: list[tuple[int, str]], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        field = self.fields["workers"]
        assert isinstance(field, forms.MultipleChoiceField)
        field.choices = [(str(pk), label) for pk, label in choices]
