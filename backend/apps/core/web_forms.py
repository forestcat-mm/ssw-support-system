"""業務画面のフォームの共通部品（見た目のクラス・二重送信の防止）。業務のルールは services。"""

import secrets
from collections.abc import Callable
from typing import Any

from django import forms
from django.http import HttpRequest

ONCE_SESSION_KEY = "once_tokens"
ONCE_FIELD = "once_token"
ONCE_KEEP = 50  # 開いたままのフォームの数の上限（古いものから捨てる）


class StyledFormMixin:
    """入力欄に部品のクラス（input / select / textarea）を付け、日付は日付の入力欄にする。"""

    fields: dict[str, forms.Field]
    add_prefix: Callable[[str], str]

    def style_fields(self) -> None:
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.DateInput):
                widget.input_type = "date"
                widget.format = "%Y-%m-%d"
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
