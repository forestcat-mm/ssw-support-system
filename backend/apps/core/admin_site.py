"""管理サイト。入れるかどうかを is_staff ではなくロールの操作（admin.access）で判定する。"""

from typing import Any

from django import forms
from django.contrib import admin
from django.contrib.admin.forms import AdminAuthenticationForm
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _

from apps.core.models import Action
from services import authorization


class RoleAdminAuthenticationForm(AdminAuthenticationForm):
    def confirm_login_allowed(self, user: Any) -> None:
        if not user.is_active or not authorization.has_action(user, Action.ADMIN_ACCESS):
            raise forms.ValidationError(
                self.error_messages["invalid_login"],
                code="invalid_login",
                params={"username": self.username_field.verbose_name},
            )


class SupportAdminSite(admin.AdminSite):
    site_header = _("Support organization management")
    site_title = _("Support organization management")
    index_title = _("Management")
    login_form = RoleAdminAuthenticationForm

    def has_permission(self, request: HttpRequest) -> bool:
        return authorization.has_action(request.user, Action.ADMIN_ACCESS)
