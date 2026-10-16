from pathlib import Path

import pytest
from pytest_django.fixtures import Settings

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
