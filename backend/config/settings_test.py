"""テスト用の設定。本番・開発の settings にテスト専用アプリと、テストだけの値を足す。"""

import os

from .settings import *  # noqa: F403
from .settings import DATABASES, INSTALLED_APPS

INSTALLED_APPS = [*INSTALLED_APPS, "tests.testapp"]

# テストだけパスワードのハッシュを速くする（本番・開発は Django 既定の PBKDF2 のまま）
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# ジョブ（Django-Q2）をその場で実行する（単体テスト・e2e にはジョブを処理するワーカーが無い）
JOBS_INLINE = True

# テスト用の DB の名前（並列のときは pytest-django がワーカーごとに _gw0 などを足す）。
# e2e は別の名前にして、単体テストが使い回す DB（--reuse-db）を消さない・取り合わない
# （docker-compose の e2e で TEST_DB_SUFFIX=e2e）
_suffix = os.environ.get("TEST_DB_SUFFIX", "")
if _suffix:
    DATABASES["default"]["TEST"] = {"NAME": f"test_{DATABASES['default']['NAME']}_{_suffix}"}
