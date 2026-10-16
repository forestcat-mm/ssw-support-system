import pytest
from django.db import IntegrityError, transaction

from apps.core.models import SupportLanguage

pytestmark = pytest.mark.django_db


def test_initial_languages_are_seeded_by_migration() -> None:
    assert list(SupportLanguage.objects.values_list("code", "name")) == [
        ("ja", "日本語"),
        ("zh-hans", "中国語（簡体字）"),
    ]


def test_code_is_unique() -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        SupportLanguage.objects.create(code="ja", name="日本語（重複）")
