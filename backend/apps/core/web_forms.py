"""業務画面のフォームの共通部品（見た目のクラス・二重送信の防止）。業務のルールは services。"""

import secrets
from collections.abc import Callable
from typing import Any

from django import forms
from django.http import HttpRequest
from django.utils import timezone
from django.utils.translation import gettext_lazy, pgettext_lazy

from services import normalize

# 入力中の形式の知らせ（forms.js。保存のときの検査は各サービスで行う）
NORMALIZE_INVALID = {
    "corporate_number": gettext_lazy(
        "The corporate number is 13 digits and the check digit must match."
    ),
    "residence_card": gettext_lazy("A residence card number is 2 letters, 8 digits and 2 letters."),
    "postal": gettext_lazy("A postal code is 7 digits."),
    "email": gettext_lazy("Check the email address."),
    "phone": gettext_lazy("A phone number is digits, hyphens and parentheses."),
    "kana": gettext_lazy("Enter it in katakana."),
    "amount": gettext_lazy("Enter the amount in digits."),
    "date": gettext_lazy("Enter the date like 2026-07-10, 2026/7/10 or R8.7.10."),
    "time": gettext_lazy("Enter the time like 09:30 or 930."),
}


class DateSelectWidget(forms.SelectDateWidget):
    """遠い日付（生年月日など）を年・月・日の選択で入れる（修正 1 回目 B16）。

    年は今年から 80 年前まで。
    """

    def __init__(self, attrs: dict[str, Any] | None = None, years_back: int = 80) -> None:
        this_year = timezone.localdate().year
        labels: Any = (  # 表示のときに訳す（遅延の文字）
            pgettext_lazy("date part", "year"),
            pgettext_lazy("date part", "month"),
            pgettext_lazy("date part", "day"),
        )
        super().__init__(
            attrs={**(attrs or {}), "class": "select select--date-part"},
            years=range(this_year, this_year - years_back - 1, -1),
            empty_label=labels,
        )


ONCE_SESSION_KEY = "once_tokens"
ONCE_FIELD = "once_token"
ONCE_KEEP = 50  # 開いたままのフォームの数の上限（古いものから捨てる）


# 入力の正規化の種類を欄の名前から決める（修正 1 回目 B13。services/normalize.py）。カナは
# 外国人の氏名のカナだけ（WorkerForm。ほかのカナの欄はふりがなをひらがなで入れることがある）
AMOUNT_SUFFIXES = ("wage", "fee", "amount", "amount_jpy", "_equivalent")


def normalize_kind(name: str, field: forms.Field) -> str:
    if name == "corporate_number":
        return "corporate_number"
    if name == "card_number":
        return "residence_card"
    if name.endswith("postal_code"):
        return "postal"
    if "phone" in name:
        return "phone"
    if isinstance(field, forms.EmailField):
        return "email"
    if isinstance(field, forms.IntegerField) and name.endswith(AMOUNT_SUFFIXES):
        return "amount"
    return ""


class StyledFormMixin:
    """入力欄に部品のクラス（input / select / textarea）を付け、日付は日付の入力欄にする。

    番号・郵便番号・電話番号・メール・カナ・金額の欄は、保存のときに services.normalize で直し、
    入力中は forms.js が同じ規則で直して欄の下に知らせる（修正 1 回目 B13）。
    """

    fields: dict[str, forms.Field]
    add_prefix: Callable[[str], str]
    data: Any
    is_bound: bool

    def normalize_field(self, name: str, kind: str) -> None:
        """欄 name を種類 kind で正規化する（名前から決まらない欄。旅券番号など）。"""
        field = self.fields[name]
        kinds: dict[str, str] = self.__dict__.setdefault("normalized_fields", {})
        kinds[name] = kind
        field.widget.attrs.update(
            {
                "data-normalize": kind,
                "data-normalize-done": gettext_lazy("Changed to “{value}”."),
                "data-normalize-invalid": NORMALIZE_INVALID.get(kind, ""),
            }
        )
        if kind == "amount" and isinstance(field.widget, forms.NumberInput):
            field.widget.input_type = "text"  # 全角・カンマも受けてから直す
            field.widget.attrs["inputmode"] = "numeric"

    def _clean_fields(self) -> None:
        kinds: dict[str, str] = self.__dict__.get("normalized_fields", {})
        if kinds and self.is_bound:
            data = self.data.copy()
            for name, kind in kinds.items():
                key = self.add_prefix(name)
                if isinstance(data.get(key), str):
                    data[key] = normalize.normalize(kind, data[key])
            self.data = data
        super()._clean_fields()  # type: ignore[misc]

    def style_fields(self) -> None:
        for name, field in self.fields.items():
            kind = normalize_kind(name, field)
            if kind:
                self.normalize_field(name, kind)
        for name, field in self.fields.items():
            widget = field.widget
            if isinstance(widget, DateSelectWidget):
                continue  # 年・月・日の選択（部品の中でクラスを付けてある）
            if isinstance(widget, forms.DateInput):
                # 日付は文字の欄＋自動の整形＋カレンダー（forms.js。修正 1 回目 B16）
                widget.input_type = "text"
                widget.format = "%Y-%m-%d"
                widget.attrs.pop("type", None)
                widget.attrs.update(
                    {
                        "inputmode": "numeric",
                        "autocomplete": "off",
                        "placeholder": "2026-07-10",
                        "data-date": "",
                        "data-calendar-label": gettext_lazy("Calendar"),
                        "data-today-label": gettext_lazy("Today"),
                        "data-close-label": gettext_lazy("Close"),
                        "data-today": timezone.localdate().isoformat(),
                    }
                )
                self.normalize_field(name, "date")
            elif isinstance(widget, forms.TimeInput):
                # 時刻は 1 つの欄で、直接入れても 5 分刻みの選択から選んでもよい（datalist。B16）
                widget.input_type = "text"
                widget.format = "%H:%M"
                widget.attrs.pop("type", None)
                widget.attrs.update(
                    {
                        "inputmode": "numeric",
                        "autocomplete": "off",
                        "placeholder": "09:30",
                        "list": "time-options",
                    }
                )
                self.normalize_field(name, "time")
            if isinstance(widget, forms.MultiWidget):
                continue  # 部品ごとにクラスを付けてある（HoursMinutesWidget・PresetOrOtherWidget）
            if isinstance(widget, forms.Select):
                css = "select"
            elif isinstance(widget, forms.Textarea):
                css = "textarea"
            elif isinstance(
                widget,
                (
                    forms.CheckboxInput,
                    forms.RadioSelect,
                    forms.ClearableFileInput,
                    forms.CheckboxSelectMultiple,
                ),
            ):
                continue  # チェックボックス・ラジオ・ファイルはブラウザの見た目のまま
            else:
                css = "input"
            widget.attrs["class"] = " ".join(filter(None, [widget.attrs.get("class"), css]))

    def enable_when(self, name: str, **conditions: str) -> None:
        """欄 name を、ほかの欄の値が conditions のときだけ入力できるようにする（forms.js）。

        条件を満たさないときは無効にして値を空にする（無効の欄は送信されない）。チェックボックスは
        チェックしたときの値が "on"。値を "*" にすると「空でない」の条件になる。JS が無いときは
        欄は有効のままなので、保存時の検査で弾くこと。
        """
        rule = " ".join(f"{self.add_prefix(other)}={value}" for other, value in conditions.items())
        self.fields[name].widget.attrs["data-enabled-when"] = rule


def issue_once_token(request: HttpRequest) -> str:
    """フォームを表示するたびに 1 回だけ使える値を発行する（二重送信の防止）。"""
    token = secrets.token_urlsafe(16)
    tokens: list[str] = list(request.session.get(ONCE_SESSION_KEY, []))
    request.session[ONCE_SESSION_KEY] = [*tokens, token][-ONCE_KEEP:]
    return token


def consume_once_token(request: HttpRequest) -> bool:
    """送信された値が未使用なら使用済みにして True。2 回目以降の送信は False。"""
    token = str(request.POST.get(ONCE_FIELD, ""))
    tokens: list[str] = list(request.session.get(ONCE_SESSION_KEY, []))
    if not token or token not in tokens:
        return False
    tokens.remove(token)
    request.session[ONCE_SESSION_KEY] = tokens
    return True


def once_context(request: HttpRequest) -> dict[str, Any]:
    return {"once_field": ONCE_FIELD, "once_token": issue_once_token(request)}


# --- 入力の部品（修正 1 回目 B1〜B4）-----------------------------------------------------


class FilteredCheckboxSelectMultiple(forms.CheckboxSelectMultiple):
    """選択肢ごとに絞り込みの値を持つチェックボックス（forms.js の data-filter-source）。

    filter_values は {選択肢の値: [絞り込みの値, ...]}。source の欄（例: 分野・事業所）で選んだ値を
    含む選択肢だけを見せ、隠した選択肢は外す。JS が無いときはすべて見え、保存の検査で弾く。
    """

    def __init__(self, *args: Any, source: str = "", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.source = source
        self.filter_values: dict[str, list[str]] = {}

    def create_option(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        option = super().create_option(*args, **kwargs)
        key = str(getattr(option["value"], "value", option["value"]))
        option["attrs"]["data-filter-source"] = self.source
        option["attrs"]["data-filter-values"] = " ".join(self.filter_values.get(key, []))
        return option


class HoursMinutesWidget(forms.MultiWidget):
    """時間と分の 2 つの欄（分の値を持つ。修正 1 回目 B3）。"""

    def __init__(self) -> None:
        super().__init__(
            [
                forms.NumberInput(attrs={"class": "input input--narrow", "min": 0, "step": 1}),
                forms.NumberInput(
                    attrs={"class": "input input--narrow", "min": 0, "max": 59, "step": 1}
                ),
            ]
        )
        self.template_name = "ui/components/hours_minutes.html"

    def decompress(self, value: Any) -> list[Any]:
        if value in (None, ""):
            return [None, None]
        hours, minutes = divmod(int(value), 60)
        return [hours, minutes]

    def value_from_datadict(self, data: Any, files: Any, name: str) -> list[Any]:
        if f"{name}_0" not in data and name in data:  # 分の 1 つの値（以前の形）
            return self.decompress(data.get(name) or None)
        return super().value_from_datadict(data, files, name)


class HoursMinutesField(forms.MultiValueField):
    """時間と分で入れ、分（整数）で返す。"""

    widget = HoursMinutesWidget

    def __init__(self, *, required: bool = True, **kwargs: Any) -> None:
        super().__init__(
            fields=[
                forms.IntegerField(min_value=0, required=False),
                forms.IntegerField(min_value=0, max_value=59, required=False),
            ],
            require_all_fields=False,
            required=required,
            **kwargs,
        )

    def compress(self, data_list: list[Any]) -> int | None:
        if not data_list or all(v in (None, "") for v in data_list):
            return None
        hours, minutes = (int(v or 0) for v in (list(data_list) + [0, 0])[:2])
        return hours * 60 + minutes


OTHER = "__other__"


class PresetOrOtherWidget(forms.MultiWidget):
    """定番の選択肢と「その他（自由入力）」（修正 1 回目 B4）。"""

    def __init__(self, presets: list[tuple[str, Any]]) -> None:
        self.presets = [value for value, _label in presets]
        super().__init__(
            [
                forms.Select(choices=[("", "---------"), *presets], attrs={"class": "select"}),
                forms.TextInput(attrs={"class": "input"}),
            ]
        )
        self.template_name = "ui/components/preset_or_other.html"

    def decompress(self, value: Any) -> list[Any]:
        if value in (None, ""):
            return ["", ""]
        if value in self.presets:
            return [value, ""]
        return [OTHER, value]

    def value_from_datadict(self, data: Any, files: Any, name: str) -> list[Any]:
        if f"{name}_0" not in data and name in data:  # 文字の 1 つの値（以前の形）
            return self.decompress(data.get(name))
        return super().value_from_datadict(data, files, name)


class PresetOrOtherField(forms.MultiValueField):
    """定番の値か、その他の文字を 1 つの文字で返す。"""

    def __init__(
        self,
        presets: list[tuple[str, Any]],
        *,
        max_length: int,
        required: bool = True,
        **kwargs: Any,
    ) -> None:
        other_label = kwargs.pop("other_label", "")
        choices = [*presets, (OTHER, other_label)]
        super().__init__(
            fields=[
                forms.ChoiceField(choices=[("", "---------"), *choices], required=False),
                forms.CharField(max_length=max_length, required=False),
            ],
            require_all_fields=False,
            required=required,
            widget=PresetOrOtherWidget(choices),
            **kwargs,
        )

    def compress(self, data_list: list[Any]) -> str:
        if not data_list:
            return ""
        choice, text = (list(data_list) + ["", ""])[:2]
        if choice == OTHER:
            return str(text or "").strip()
        return str(choice or "")


class StaffPeriodForm(StyledFormMixin, forms.Form):
    """担当者・支援責任者を変える（新しい人・開始日・理由。T45）。検査は services.staff_periods。"""

    staff: forms.ModelChoiceField[Any] = forms.ModelChoiceField(queryset=None, required=False)
    starts_on = forms.DateField(label=gettext_lazy("from"), widget=forms.DateInput)
    reason = forms.CharField(
        label=gettext_lazy("reason for the change"),
        max_length=200,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text=gettext_lazy("Example: handed over because of a transfer."),
    )

    def __init__(
        self,
        *args: Any,
        queryset: Any,
        label: Any,
        empty_label: Any = None,
        help_text: Any = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        staff = self.fields["staff"]
        assert isinstance(staff, forms.ModelChoiceField)
        staff.queryset = queryset
        staff.label = label
        staff.empty_label = empty_label
        staff.required = empty_label is None
        staff.help_text = help_text
        self.fields["starts_on"].help_text = gettext_lazy(
            "The day the new person takes over. A past date is allowed; a future date is not."
        )
        if not self.is_bound:
            self.initial.setdefault("starts_on", timezone.localdate())
        self.style_fields()


class PeriodVoidForm(StyledFormMixin, forms.Form):
    reason = forms.CharField(
        label=gettext_lazy("void reason"),
        max_length=200,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=gettext_lazy("Example: the start date was entered incorrectly."),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()
