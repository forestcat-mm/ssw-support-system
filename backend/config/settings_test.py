"""テスト用の設定。本番・開発の settings にテスト専用アプリを足すだけ。"""

from .settings import *  # noqa: F403
from .settings import INSTALLED_APPS

INSTALLED_APPS = [*INSTALLED_APPS, "tests.testapp"]
