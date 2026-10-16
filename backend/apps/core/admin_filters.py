"""admin の一覧フィルタ。"""

from collections.abc import Iterator
from datetime import date
from typing import Any

from django.contrib import admin
from django.contrib.admin.views.main import ChangeList
from django.db.models import DateTimeField, Model, QuerySet
from django.http import HttpRequest, QueryDict


class DateRangeFilter(admin.FieldListFilter):
    """日付・日時の列を開始日・終了日（両端を含む。日時は Asia/Tokyo の日付）で絞り込む。"""

    template = "admin/core/date_range_filter.html"

    def __init__(
        self,
        field: Any,
        request: HttpRequest,
        params: dict[str, Any],
        model: type[Model],
        model_admin: admin.ModelAdmin[Any],
        field_path: str,
    ) -> None:
        date_path = f"{field_path}__date" if isinstance(field, DateTimeField) else field_path
        self.lookup_since = f"{date_path}__gte"
        self.lookup_until = f"{date_path}__lte"
        super().__init__(field, request, params, model, model_admin, field_path)

    def expected_parameters(self) -> list[str | None]:
        return [self.lookup_since, self.lookup_until]

    def has_output(self) -> bool:
        return True

    def queryset(self, request: HttpRequest, queryset: QuerySet[Any]) -> QuerySet[Any]:
        for lookup in (self.lookup_since, self.lookup_until):
            value = _parse_date(self.used_parameters.get(lookup))
            if value is not None:
                queryset = queryset.filter(**{lookup: value})
        return queryset

    # 標準の選択肢（selected / query_string / display）ではなく、
    # 日付入力欄のテンプレート用の値を返す
    def choices(self, changelist: ChangeList) -> Iterator[dict[str, Any]]:  # type: ignore[override]
        others = QueryDict(
            changelist.get_query_string(remove=[self.lookup_since, self.lookup_until])[1:]
        )
        yield {
            "since_name": self.lookup_since,
            "since_value": _value_of(self.used_parameters.get(self.lookup_since)),
            "until_name": self.lookup_until,
            "until_value": _value_of(self.used_parameters.get(self.lookup_until)),
            "hidden": [(key, value) for key in others for value in others.getlist(key)],
        }


def _value_of(raw: Any) -> str:
    if isinstance(raw, list):
        raw = raw[-1] if raw else ""
    return str(raw or "")


def _parse_date(raw: Any) -> date | None:
    try:
        return date.fromisoformat(_value_of(raw))
    except ValueError:
        return None
