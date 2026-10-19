import os
from datetime import datetime, timedelta

import pytest
from django.conf import settings
from django.contrib.sessions.models import Session
from django.utils import timezone

from apps.core.models import User
from services import sessions

from .factories import make_user, verified_client

pytestmark = pytest.mark.django_db


def test_idle_timeout_settings() -> None:
    # 無操作 60 分・ログインから 12 時間・5 分前に知らせる（修正 1 回目 J8。
    # NIST SP 800-63B-4 2.2.3）
    idle = int(os.environ.get("SESSION_IDLE_TIMEOUT_MINUTES", "60"))  # 環境変数（既定 60）
    assert settings.SESSION_COOKIE_AGE == idle * 60
    assert settings.SESSION_ABSOLUTE_TIMEOUT == 12 * 3600
    assert settings.SESSION_WARNING_SECONDS == 5 * 60
    assert settings.SESSION_SAVE_EVERY_REQUEST is True
    assert settings.SESSION_EXPIRE_AT_BROWSER_CLOSE is True


def _session_of(client_session_key: str) -> Session:
    return Session.objects.get(session_key=client_session_key)


def test_each_request_extends_the_session() -> None:
    user: User = make_user("tanto", roles=("support_staff",))
    client = verified_client(user)
    key = client.session.session_key
    assert key is not None
    Session.objects.filter(session_key=key).update(
        expire_date=timezone.now() + timedelta(minutes=1)
    )

    assert client.get("/admin/").status_code == 200

    remaining = _session_of(key).expire_date - timezone.now()
    assert remaining > timedelta(seconds=settings.SESSION_COOKIE_AGE - 60)


def test_idle_session_expires() -> None:
    user: User = make_user("tanto", roles=("support_staff",))
    client = verified_client(user)
    key = client.session.session_key
    Session.objects.filter(session_key=key).update(
        expire_date=timezone.now() - timedelta(seconds=1)
    )
    response = client.get("/admin/", follow=True)
    assert response.redirect_chain[-1][0].startswith("/accounts/login/")
    assert "_auth_user_id" not in client.session


def test_session_ends_twelve_hours_after_sign_in() -> None:
    """ログインからの上限は延ばせない（リクエストがあってもログアウトする。J8）。"""
    user: User = make_user("tanto", roles=("support_staff",))
    client = verified_client(user)
    session = client.session
    started = datetime.fromisoformat(session[sessions.LOGIN_AT_KEY])
    assert abs((timezone.now() - started).total_seconds()) < 60  # ログインで数え始める
    assert client.get("/workers/").status_code == 200
    session = client.session
    session[sessions.LOGIN_AT_KEY] = (timezone.now() - timedelta(hours=12, seconds=1)).isoformat()
    session.save()
    response = client.get("/workers/")
    assert response.status_code == 302 and response["Location"].startswith("/accounts/login/")
    assert "_auth_user_id" not in client.session
    page = client.get(response["Location"])
    assert "12 時間" in page.content.decode()


def test_keepalive_after_the_limit_is_401() -> None:
    user: User = make_user("tanto", roles=("support_staff",))
    client = verified_client(user)
    assert client.post("/accounts/keepalive/").status_code == 204
    session = client.session
    session[sessions.LOGIN_AT_KEY] = (timezone.now() - timedelta(hours=13)).isoformat()
    session.save()
    assert client.post("/accounts/keepalive/").status_code == 401
    assert client.post("/accounts/keepalive/").status_code == 401  # ログアウトした後


def test_pages_carry_the_session_timing() -> None:
    user: User = make_user("tanto", roles=("support_staff",))
    client = verified_client(user)
    page = client.get("/workers/")
    timing = page.context["session_timing"]
    assert timing["idle"] == settings.SESSION_COOKIE_AGE and timing["warning"] == 300
    assert 12 * 3600 - 60 < timing["absolute"] <= 12 * 3600
    assert 'id="session-notice"' in page.content.decode()


def test_settings_default_to_sixty_minutes_and_twelve_hours() -> None:
    """環境変数が無いときの既定（.env.example も同じ値）。"""
    source = (settings.BASE_DIR / "config" / "settings.py").read_text(encoding="utf-8")
    assert 'env.int("SESSION_IDLE_TIMEOUT_MINUTES", default=60)' in source
    assert 'env.int("SESSION_ABSOLUTE_TIMEOUT_HOURS", default=12)' in source
