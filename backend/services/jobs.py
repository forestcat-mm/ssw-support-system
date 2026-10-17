"""ジョブの登録（Django-Q2。ADR-0005）。

JOBS_INLINE が真ならその場で実行する（単体テストと e2e。ジョブを処理するワーカーが無いため）。
本番と開発では偽で、qcluster が非同期に実行する。引数は JSON にできる値だけを渡す。
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.utils.module_loading import import_string


def enqueue(func_path: str, *args: Any) -> None:
    if settings.JOBS_INLINE:
        import_string(func_path)(*args)
        return
    from django_q.tasks import async_task  # type: ignore[import-untyped]

    async_task(func_path, *args)
