from pathlib import Path

import pytest
from django.test import Client
from pytest_django.fixtures import Settings

from apps.core.models import User
from tests.core.factories import make_user, verified_client

# 時間のかかるテスト（make check-quick では飛ばし、make test では流す）。LibreOffice で PDF に
# 変換するモジュールと、ダミーデータを丸ごと作る（seed_dummy）テスト。
SLOW_MODULES = {
    "tests/forms_engine/test_checks.py",
    "tests/forms_engine/test_form_1_17.py",
    "tests/forms_engine/test_form_1_26.py",
    "tests/forms_engine/test_form_application.py",
    "tests/forms_engine/test_form_briefing.py",
    "tests/forms_engine/test_form_career.py",
    "tests/forms_engine/test_form_contract.py",
    "tests/forms_engine/test_form_interview.py",
    "tests/forms_engine/test_form_languages.py",
    "tests/forms_engine/test_generation.py",
    "tests/forms_engine/test_pdf_merges.py",
    "tests/forms_engine/test_pdfcheck.py",
    "tests/forms_engine/test_seed_documents.py",
    "tests/forms_engine/test_submission_lists.py",
    "tests/forms_engine/test_translations.py",
    "tests/seed/test_seed_dummy.py",
    "tests/seed/test_seed_scenario.py",
    "tests/seed/test_seed_support.py",
}
SLOW_FIXTURES = {"seeded"}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        module = item.nodeid.split("::")[0]
        uses = set(getattr(item, "fixturenames", ()))
        if module in SLOW_MODULES or uses & SLOW_FIXTURES:
            item.add_marker(pytest.mark.slow)


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
