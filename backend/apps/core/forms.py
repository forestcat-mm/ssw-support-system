from django import forms
from django.utils.translation import gettext_lazy as _


class TokenForm(forms.Form):
    token = forms.CharField(
        label=_("6-digit code"),
        max_length=6,
        min_length=6,
        widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "inputmode": "numeric"}),
    )


class SecondFactorForm(forms.Form):
    code = forms.CharField(
        label=_("6-digit code or recovery code"),
        max_length=20,
        widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "autofocus": True}),
    )
