from pathlib import Path

import pytest
from django.test import Client
from pytest_django.fixtures import Settings

from apps.core.models import User
from tests.core.factories import make_user, verified_client

PDF_BYTES = b"%PDF-1.4\n% test\n"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture(autouse=True)
def _media_storage(settings: Settings, tmp_path: Path) -> None:
    """アップロード先をテストごとの一時ディレクトリにする。"""
    settings.STORAGES = {
        **settings.STORAGES,
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path / "media")},
        },
    }


@pytest.fixture
def admin_user(db: None) -> User:
    """管理者ロールのユーザー（pytest-django の superuser 版を置き換える）。"""
    return make_user("admin", roles=("admin",))


@pytest.fixture
def admin_client(admin_user: User) -> Client:
    return verified_client(admin_user)


@pytest.fixture
def support_staff_user(db: None) -> User:
    return make_user("tanto", roles=("support_staff",))


@pytest.fixture
def support_staff_client(support_staff_user: User) -> Client:
    return verified_client(support_staff_user)
