"""一括届出の画面のフォーム（T50・T52。ADR-0040）。検査・生成は services/bulk_filings。"""

from __future__ import annotations

import datetime
from typing import Any

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.web_forms import StyledFormMixin
from apps.forms_engine.models import BulkFilingRow
from apps.hosts.models import HostOrganization
from services import bulk_filings, efile_data


class BulkFilingForm(StyledFormMixin, forms.Form):
    form_id = forms.ChoiceField(
        label=_("notification"),
        choices=[(form_id, bulk_filings.label_of(form_id)) for form_id in efile_data.FORM_IDS],
    )
    host = forms.ModelChoiceField(
        label=_("host organization"),
        queryset=HostOrganization.objects.none(),
        required=False,
        help_text=_("The filer of forms 3-1 to 3-7. Not used for form 4-3 (own organization)."),
    )
    title = forms.CharField(label=_("title"), max_length=100, required=False)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        host = self.fields["host"]
        assert isinstance(host, forms.ModelChoiceField)
        host.queryset = HostOrganization.objects.order_by("name_kana", "name")
        self.style_fields()

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        form_id = cleaned.get("form_id", "")
        if form_id and form_id not in efile_data.ORGANIZATION_FORMS and not cleaned.get("host"):
            self.add_error("host", _("Choose the host organization."))
        return cleaned


def _choices(table: dict[str, Any], target: str) -> list[str]:
    """入力規則のリストの値（連動リストは、親の値に合うすべての表の値。組み合わせは検査で確かめる）。"""
    spec = table["lists"].get(target)
    if spec is None:
        return []
    if spec["kind"] == "static":
        return list(spec["values"])
    parent = table["lists"].get(spec["parent"], {})
    names = parent.get("values", []) if parent.get("kind") == "static" else []
    values: list[str] = []
    for name in names:
        key = name
        if spec.get("substitute"):
            key = key.replace("（", "_").replace("）", "_").replace("，", "_")
        for value in table["linked"].get(key + spec.get("suffix", ""), []):
            if value not in values:
                values.append(value)
    return values


class RowForm(StyledFormMixin, forms.Form):
    """1 行の届出の内容の欄（e.*）。本人の欄・届出機関・登録支援機関の欄は台帳から作る。"""

    def __init__(
        self, *args: Any, row: BulkFilingRow, definition: dict[str, Any], **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self.row = row
        table = definition["columns_table"]
        lengths = {column["column"]: column.get("length") for column in table["columns"]}
        estimated = set((row.inputs or {}).get(efile_data.ESTIMATED, []))
        self.estimated_names: list[str] = []
        self.keys: dict[str, str] = {}
        for index, field in enumerate(definition["fields"]):
            if not field["key"].startswith("e."):
                continue
            name = f"f{index}"
            self.keys[name] = field["key"]
            help_text: Any = ""
            if field.get("advice_length"):
                help_text = _(
                    "The heading of the form says up to %(count)s full-width characters."
                ) % {"count": field["advice_length"]}
            if field["key"] in estimated:
                self.estimated_names.append(name)
                help_text = _("Estimated from the filing task. Check it.")
            if field["format"] == "date3":
                self.fields[name] = forms.DateField(
                    label=field["label"],
                    required=False,
                    widget=forms.DateInput,
                    help_text=help_text,
                )
            elif field.get("choices"):
                self.fields[name] = forms.ChoiceField(
                    label=field["label"],
                    choices=[("", "---------")]
                    + [(value, value) for value in _choices(table, field["target"])],
                    required=False,
                    help_text=help_text,
                )
            else:
                length = lengths.get(field["target"])
                self.fields[name] = forms.CharField(
                    label=field["label"],
                    max_length=length if length and length < 9999 else None,
                    required=False,
                    widget=forms.Textarea(attrs={"rows": 2}) if (length or 0) > 100 else None,
                    help_text=help_text,
                )
            value = (row.inputs or {}).get(field["key"], "")
            if field["format"] == "date3" and isinstance(value, str) and value:
                value = datetime.date.fromisoformat(value)
            self.initial[name] = value
        self.style_fields()

    def inputs(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, key in self.keys.items():
            value = self.cleaned_data.get(name)
            if value not in (None, ""):
                result[key] = value
        return result


class AddFilingsForm(forms.Form):
    filings = forms.MultipleChoiceField(
        label=_("filing tasks"), widget=forms.CheckboxSelectMultiple, required=True
    )

    def __init__(self, *args: Any, choices: list[tuple[int, str]], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        field = self.fields["filings"]
        assert isinstance(field, forms.MultipleChoiceField)
        field.choices = [(str(pk), label) for pk, label in choices]


class SubmissionForm(StyledFormMixin, forms.Form):
    """電子届出の申込の記録（T52-D2）。行のすべての届出タスクを提出済みにする。"""

    submitted_on = forms.DateField(label=_("submitted on"), widget=forms.DateInput)
    receipt_number = forms.CharField(label=_("receipt number"), max_length=50, required=False)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial.setdefault("submitted_on", timezone.localdate())
        self.style_fields()
