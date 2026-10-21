"""契約・事業所の入力の部品（修正 1 回目 B1〜B4）。"""

import datetime
from collections.abc import Callable

import pytest

from apps.contracts.forms import VersionForm
from apps.core.web_forms import (
    FilteredCheckboxSelectMultiple,
    HoursMinutesField,
    HoursMinutesWidget,
    PresetOrOtherField,
)
from apps.hosts.forms import HostSiteForm
from tests.hosts.factories import category, field, make_host, make_site

pytestmark = pytest.mark.django_db


def test_site_categories_are_filtered_by_field(
    today_is: Callable[[datetime.date], None],
) -> None:
    # 「飲食料品製造業全般」は 2027-03-31 まで。選択肢は今日に適用中の業務区分
    today_is(datetime.date(2026, 10, 16))
    form = HostSiteForm()
    widget = form.fields["categories"].widget
    assert isinstance(widget, FilteredCheckboxSelectMultiple) and widget.source == "id_field"
    food = category("food_manufacturing_general")
    assert widget.filter_values[str(food.pk)] == [str(field("food_manufacturing").pk)]
    assert 'data-filter-source="id_field"' in str(form["categories"])


def test_contract_categories_are_filtered_by_site() -> None:
    host = make_host()
    machine = make_site(host, name="本社工場")
    machine.categories.set([category("machine_metal_processing")])
    sewing = make_site(host, name="第二工場")
    sewing.categories.set([category("sewing"), category("machine_metal_processing")])
    form = VersionForm()
    widget = form.fields["categories"].widget
    assert isinstance(widget, FilteredCheckboxSelectMultiple) and widget.source == "id_site"
    assert sorted(widget.filter_values[str(category("machine_metal_processing").pk)]) == sorted(
        [str(machine.pk), str(sewing.pk)]
    )
    assert widget.filter_values[str(category("sewing").pk)] == [str(sewing.pk)]


def test_hours_and_minutes_are_stored_as_minutes() -> None:
    field_ = HoursMinutesField()
    assert field_.clean(["7", "45"]) == 465
    widget = HoursMinutesWidget()
    assert widget.decompress(465) == [7, 45]
    # 以前の形（分の 1 つの値）でも受ける
    assert widget.value_from_datadict({"x": "480"}, {}, "x") == [8, 0]
    assert HoursMinutesField(required=False).clean(["", ""]) is None


def test_preset_or_other() -> None:
    field_ = PresetOrOtherField([("毎月末日", "毎月末日")], max_length=30)
    assert field_.clean(["毎月末日", ""]) == "毎月末日"
    assert field_.clean(["__other__", " 翌々月10日 "]) == "翌々月10日"
    assert field_.widget.decompress("翌々月10日") == ["__other__", "翌々月10日"]
    assert field_.widget.value_from_datadict({"d": "毎月末日"}, {}, "d") == ["毎月末日", ""]
