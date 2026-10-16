"""業務画面の権限。

判定は services.authorization に集約し、ここは画面ごとに必要な操作を宣言するだけ。
"""

from typing import Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponseBase

from apps.core.models import Action
from services import authorization


class ActionRequiredMixin(LoginRequiredMixin):
    """ログインと TOTP 検証（MFARequiredMiddleware）に加え、
    required_action を持つロールだけ通す。
    """

    required_action: Action | None = None

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if self.required_action is not None and not authorization.has_action(
            request.user, self.required_action
        ):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)
