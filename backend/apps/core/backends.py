"""認証バックエンド。パスワード認証は Django 標準、権限はロールで判定する。

Django 標準のグループ・ユーザー個別権限は使わない（権限の仕組みを二重にしない）。
"""

from typing import Any

from django.contrib.auth.backends import ModelBackend
from django.db.models import Model

from services import authorization


class RoleBackend(ModelBackend):
    def get_user_permissions(self, user_obj: Any, obj: Model | None = None) -> set[str]:
        return set()

    def get_group_permissions(self, user_obj: Any, obj: Model | None = None) -> set[str]:
        return set()

    def get_all_permissions(self, user_obj: Any, obj: Model | None = None) -> set[str]:
        return authorization.django_permissions_of(user_obj)

    def has_perm(self, user_obj: Any, perm: str, obj: Model | None = None) -> bool:
        return authorization.has_django_permission(user_obj, perm, obj)

    def has_module_perms(self, user_obj: Any, app_label: str) -> bool:
        return authorization.has_django_module_permission(user_obj, app_label)
