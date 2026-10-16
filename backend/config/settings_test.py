"""テスト用の設定。本番・開発の settings にテスト専用アプリを足すだけ。"""

from .settings import *  # noqa: F403
from .settings import INSTALLED_APPS

INSTALLED_APPS = [*INSTALLED_APPS, "tests.testapp"]

# テストだけパスワードのハッシュを速くする（本番・開発は Django 既定の PBKDF2 のまま）
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
