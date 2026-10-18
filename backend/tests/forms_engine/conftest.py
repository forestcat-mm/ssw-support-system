import pytest

from services import form_templates


@pytest.fixture
def loaded(db: None) -> None:
    """リポジトリの様式（第1-17号・第5-9号の見本）を読み込む。"""
    form_templates.load_all()
