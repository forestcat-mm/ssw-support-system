"""基盤の疎通確認（T02）。T01 の完了条件を自動で再確認する。"""

import pytest
from django.conf import settings
from django.test import Client


def test_locale_and_timezone_settings() -> None:
    assert settings.LANGUAGE_CODE == "ja"
    assert settings.TIME_ZONE == "Asia/Tokyo"
    assert settings.USE_TZ is True


@pytest.mark.django_db
def test_login_page_is_served(client: Client) -> None:
    response = client.get("/accounts/login/")
    assert response.status_code == 200
