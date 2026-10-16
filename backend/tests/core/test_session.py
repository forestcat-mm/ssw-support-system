from datetime import timedelta

import pytest
from django.conf import settings
from django.contrib.sessions.models import Session
from django.utils import timezone

from apps.core.models import User

from .factories import make_user, verified_client

pytestmark = pytest.mark.django_db


def test_idle_timeout_settings() -> None:
    assert settings.SESSION_COOKIE_AGE == 30 * 60
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
    assert remaining > timedelta(minutes=29)


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
