"""ログインのセッションの期限（修正 1 回目 J8、ADR-0030）。

- 無操作の失効: 最後のリクエストから SESSION_COOKIE_AGE 秒（環境変数 SESSION_IDLE_TIMEOUT_MINUTES。
  既定 60 分）。リクエストのたびに延びる（SESSION_SAVE_EVERY_REQUEST）。
- ログインからの上限: SESSION_ABSOLUTE_TIMEOUT 秒（環境変数 SESSION_ABSOLUTE_TIMEOUT_HOURS。既定
  12 時間）。延長できない。ログインの日時をセッションに持ち、ミドルウェアで確かめる。
- 画面は失効の SESSION_WARNING_SECONDS 秒前（5 分前）に知らせる（無操作なら「延長する」で
  延ばせる）。
根拠は NIST SP 800-63B-4 2.2.3（AAL2: 全体の上限は 24 時間以下、無操作は 1 時間以下が推奨）。
"""

from __future__ import annotations

import datetime
from typing import Any

from django.conf import settings
from django.utils import timezone

LOGIN_AT_KEY = "login_at"


def login_at(session: Any, now: datetime.datetime | None = None) -> datetime.datetime:
    """ログインの日時（持っていない古いセッションは、今をログインの日時とする）。"""
    value = session.get(LOGIN_AT_KEY)
    if value:
        return datetime.datetime.fromisoformat(value)
    now = now or timezone.now()
    session[LOGIN_AT_KEY] = now.isoformat()
    return now


def started(session: Any, now: datetime.datetime | None = None) -> None:
    """ログインした（上限を数え始める）。"""
    session[LOGIN_AT_KEY] = (now or timezone.now()).isoformat()


def absolute_remaining(session: Any, now: datetime.datetime | None = None) -> int:
    """ログインからの上限までの秒（0 以下なら過ぎている）。"""
    now = now or timezone.now()
    end = login_at(session, now) + datetime.timedelta(seconds=settings.SESSION_ABSOLUTE_TIMEOUT)
    return int((end - now).total_seconds())


def timing(session: Any, now: datetime.datetime | None = None) -> dict[str, int]:
    """画面の知らせに使う秒（無操作の失効・知らせを出す前・上限までの残り）。"""
    return {
        "idle": int(settings.SESSION_COOKIE_AGE),
        "warning": int(settings.SESSION_WARNING_SECONDS),
        "absolute": max(absolute_remaining(session, now), 0),
    }
