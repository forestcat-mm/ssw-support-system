"""事前ガイダンス・生活オリエンテーションの通訳（修正 1 回目 B7）。"""

from typing import Any

import pytest
from django.core.exceptions import ValidationError

from apps.core.models import SupportLanguage
from apps.support.models import Briefing, InterpreterKind
from tests.support.test_briefings import create, keys, orientation
from tests.support.test_briefings import worker as worker  # noqa: F401  （fixture）

pytestmark = pytest.mark.django_db


def test_someone_else_needs_the_name_and_the_language(worker: Any) -> None:  # noqa: F811
    with pytest.raises(ValidationError) as error:
        create(worker, orientation(worker, interpreter_kind=InterpreterKind.OTHER))
    assert {"interpreter", "interpreter_language"} <= keys(error)
    chinese = SupportLanguage.objects.get(code="zh-hans")
    briefing = create(
        worker,
        orientation(
            worker,
            interpreter_kind=InterpreterKind.OTHER,
            interpreter="通訳 太郎（〇〇通訳）",
            interpreter_language=chinese,
        ),
    )
    assert briefing.interpretation == InterpreterKind.OTHER
    assert briefing.interpreter_language == chinese


def test_staff_or_none_clears_the_interpreter(worker: Any) -> None:  # noqa: F811
    briefing = create(
        worker,
        orientation(worker, interpreter_kind=InterpreterKind.STAFF, interpreter="残る文字"),
    )
    assert briefing.interpreter == "" and briefing.interpreter_language is None
    assert briefing.interpretation == InterpreterKind.STAFF


def test_old_records_are_read_from_the_name() -> None:
    assert Briefing(interpreter="通訳 太郎").interpretation == InterpreterKind.OTHER
    assert Briefing(interpreter="").interpretation == InterpreterKind.NONE
