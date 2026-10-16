"""業務画面のフォームの共通部品（見た目のクラス・二重送信の防止）。業務のルールは services。"""

import secrets
from typing import Any

from django import forms
from django.http import HttpRequest

ONCE_SESSION_KEY = "once_tokens"
ONCE_FIELD = "once_token"
ONCE_KEEP = 50  # 開いたままのフォームの数の上限（古いものから捨てる）


class StyledFormMixin:
    """入力欄に部品のクラス（input / select / textarea）を付け、日付は日付の入力欄にする。"""

    fields: dict[str, forms.Field]

    def style_fields(self) -> None:
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.DateInput):
                widget.input_type = "date"
                widget.format = "%Y-%m-%d"
            if isinstance(widget, forms.Select):
                css = "select"
            elif isinstance(widget, forms.Textarea):
                css = "textarea"
            elif isinstance(
                widget, (forms.CheckboxInput, forms.RadioSelect, forms.ClearableFileInput)
            ):
                continue  # チェックボックス・ラジオ・ファイルはブラウザの見た目のまま
            else:
                css = "input"
            widget.attrs["class"] = " ".join(filter(None, [widget.attrs.get("class"), css]))


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
