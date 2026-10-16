"""外国人台帳のテスト用ヘルパ。"""

from datetime import date
from typing import Any

from apps.core.models import Country, SupportLanguage
from apps.workers.models import (
    ApplicationType,
    ForeignWorker,
    PassportRecord,
    ResidenceRecord,
    Sex,
    StatusOfResidence,
)


def make_worker(**overrides: Any) -> ForeignWorker:
    values: dict[str, Any] = {
        "name_romaji": "WANG FANG",
        "name_kanji": "王芳",
        "nationality": Country.objects.get(code="CN"),
        "birth_date": date(1998, 5, 1),
        "sex": Sex.FEMALE,
        "support_language": SupportLanguage.objects.get(code="zh-hans"),
    }
    return ForeignWorker.objects.create(**(values | overrides))


def add_residence(worker: ForeignWorker, **overrides: Any) -> ResidenceRecord:
    values: dict[str, Any] = {
        "card_number": "AB12345678CD",
        "status_of_residence": StatusOfResidence.SSW_1,
        "permitted_on": date(2025, 4, 1),
        "expires_on": date(2026, 4, 1),
        "application_type": ApplicationType.CHANGE,
    }
    return ResidenceRecord.objects.create(worker=worker, **(values | overrides))


def add_passport(worker: ForeignWorker, **overrides: Any) -> PassportRecord:
    values: dict[str, Any] = {
        "number": "E12345678",
        "issuing_country": Country.objects.get(code="CN"),
        "issued_on": date(2020, 1, 10),
        "expires_on": date(2030, 1, 9),
    }
    return PassportRecord.objects.create(worker=worker, **(values | overrides))
