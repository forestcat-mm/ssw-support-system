import datetime
import shutil
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from pytest_django.fixtures import Settings as SettingsWrapper

from apps.seed.models import SeedObject
from apps.workers.models import ForeignWorker
from services import form_templates

BASE = datetime.date(2026, 10, 16)


@pytest.fixture
def loaded(db: None) -> None:
    """リポジトリの様式（第1-17号・第5-9号の見本）を読み込む。"""
    form_templates.load_all()


@pytest.fixture
def repo_copy(tmp_path: Path, settings: SettingsWrapper) -> tuple[Path, Path]:
    """リポジトリの様式とマッピングを一時の場所に写して読む（書き換えの試験用）。"""
    templates = tmp_path / "forms"
    mappings = tmp_path / "mappings"
    shutil.copytree(settings.FORM_TEMPLATES_ROOT, templates)
    shutil.copytree(settings.FORM_MAPPINGS_ROOT, mappings)
    settings.FORM_TEMPLATES_ROOT = templates
    settings.FORM_MAPPINGS_ROOT = mappings
    return templates, mappings


@pytest.fixture
def seeded(settings: SettingsWrapper, loaded: None) -> list[ForeignWorker]:
    settings.DEBUG = True
    settings.ALLOW_DUMMY_DATA = True
    call_command("seed_dummy", "--date", BASE.isoformat(), stdout=StringIO())
    ids = SeedObject.objects.filter(content_type__model="foreignworker").values("object_id")
    return list(ForeignWorker.objects.filter(pk__in=ids).order_by("pk"))
