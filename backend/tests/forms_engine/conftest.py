import shutil
from pathlib import Path

import pytest
from pytest_django.fixtures import Settings as SettingsWrapper

from services import form_templates


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
