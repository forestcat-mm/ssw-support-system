"""列暗号化のフィールド型（T07 以降の在留カード番号・パスポート番号・口座情報で使う）。

どちらの型も audit_mask = True を持ち、監査ログには "***" だけが残る（services.audit）。
"""

from __future__ import annotations

from typing import Any

from django import forms
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import Lookup, Transform

from services import crypto


class EncryptedCharField(models.TextField):  # type: ignore[type-arg]
    """値を Fernet で暗号化して保存する。max_length は平文の長さとしてフォームで検査する。

    暗号文は毎回変わるため、isnull 以外の検索はできない（検索はブラインドインデックスで行う）。
    """

    audit_mask = True
    description = "Encrypted text"

    def from_db_value(self, value: Any, expression: Any, connection: Any) -> str | None:
        if value is None:
            return None
        if value == "":
            return ""
        return crypto.decrypt(str(value))

    def to_python(self, value: Any) -> str | None:
        if value is None or isinstance(value, str):
            return value
        return str(value)

    def get_prep_value(self, value: Any) -> str | None:
        prepared: str | None = super().get_prep_value(value)
        if prepared is None or prepared == "":
            return prepared
        return crypto.encrypt(prepared)

    def get_lookup(self, lookup_name: str) -> type[Lookup[Any]] | None:
        if lookup_name == "isnull":
            return super().get_lookup(lookup_name)
        return None

    def get_transform(self, lookup_name: str) -> type[Transform] | None:
        return None

    def formfield(
        self,
        form_class: type[forms.Field] | None = None,
        choices_form_class: type[forms.ChoiceField] | None = None,
        **kwargs: Any,
    ) -> forms.Field | None:
        # TextField の Textarea ではなく、1 行の入力欄（平文の max_length 付き）にする。
        # admin は TextField に AdminTextareaWidget を渡してくるので置き換える
        widget = kwargs.get("widget")
        if _is_textarea(widget):
            kwargs["widget"] = forms.TextInput(attrs={"class": "vTextField"})
        return models.Field.formfield(
            self,
            form_class=form_class or forms.CharField,
            choices_form_class=choices_form_class,
            **{"max_length": self.max_length, **kwargs},
        )


class BlindIndexField(models.CharField):  # type: ignore[type-arg]
    """source の列（平文）から検索用の HMAC を保存する。完全一致の検索に使う。

    例: filter(card_number_index=crypto.blind_index(入力値))
    """

    audit_mask = True
    description = "Blind index (HMAC-SHA256)"

    def __init__(self, *args: Any, source: str, **kwargs: Any) -> None:
        self.source = source
        kwargs.setdefault("max_length", 64)
        kwargs.setdefault("db_index", True)
        kwargs.setdefault("null", True)
        kwargs.setdefault("blank", True)
        kwargs.setdefault("editable", False)
        super().__init__(*args, **kwargs)

    def deconstruct(self) -> Any:
        name, path, args, kwargs = super().deconstruct()
        kwargs["source"] = self.source
        return name, path, args, kwargs

    def pre_save(self, model_instance: models.Model, add: bool) -> str | None:
        value = crypto.blind_index(getattr(model_instance, self.source))
        setattr(model_instance, self.attname, value)
        return value


def _is_textarea(widget: Any) -> bool:
    if isinstance(widget, type):
        return issubclass(widget, forms.Textarea)
    return isinstance(widget, forms.Textarea)


class ChoiceArrayField(ArrayField):  # type: ignore[type-arg]
    """選択肢の複数選択（PostgreSQL の配列）。フォームではチェックボックスの一覧にする。

    例: 雇用条件書の「更新の判断基準」「休日の曜日」「社会保険の加入状況」。
    """

    def formfield(
        self,
        form_class: type[forms.Field] | None = None,
        choices_form_class: type[forms.ChoiceField] | None = None,
        **kwargs: Any,
    ) -> Any:
        defaults: dict[str, Any] = {
            "form_class": form_class or forms.TypedMultipleChoiceField,
            "choices": self.base_field.choices,
            "coerce": self.base_field.to_python,
            "widget": forms.CheckboxSelectMultiple,
            "required": not self.blank,
        }
        defaults.update(kwargs)
        # ArrayField.formfield は SimpleArrayField を使うため、Field.formfield を直接呼ぶ
        return models.Field.formfield(self, **defaults)
