from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.core.models import Role, Staff, User


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


class InvitationForm(forms.ModelForm[User]):
    """admin の「ユーザーを追加」。パスワードは入力させず、招待メールで本人に設定させる。"""

    roles = forms.ModelMultipleChoiceField(
        label=_("roles"), queryset=Role.objects.all(), widget=forms.CheckboxSelectMultiple
    )
    staff = forms.ModelChoiceField(
        label=_("staff member"),
        queryset=Staff.objects.filter(user__isnull=True, is_active=True),
        required=False,
        help_text=_("Link this user to a staff member who has no login yet (optional)."),
    )

    class Meta:
        model = User
        fields = ("username", "email", "last_name", "first_name")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["email"].required = True

    def clean_email(self) -> str:
        email: str = self.cleaned_data["email"].strip()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError(_("A user with this email address already exists."))
        return email
