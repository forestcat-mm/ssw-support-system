"""入力の正規化（修正 1 回目 B13）。

保存のときにフォームで直し、入力中は forms.js が同じ規則で直す。
"""

import pytest
from django import forms

from apps.core.web_forms import StyledFormMixin, normalize_kind
from apps.hosts.forms import HostOrganizationForm
from services import normalize


@pytest.mark.parametrize(
    ("kind", "value", "expected"),
    [
        ("corporate_number", "７０００－０１２０５０００２", "7000012050002"),
        ("corporate_number", " 7000-0120-50002 ", "7000012050002"),
        ("residence_card", "ａｂ１２３４５６７８ｃｄ", "AB12345678CD"),
        ("passport", "e 1234 5678", "E12345678"),
        ("postal", "１００００01", "100-0001"),
        ("postal", "〒100－0001", "100-0001"),
        ("postal", "100-00", "100-00"),  # 7 桁でなければ形は変えない（検査で知らせる）
        ("phone", "０３ー１２３４－５６７８", "03-1234-5678"),
        ("phone", " +86 10 1234 ", "+86 10 1234"),
        ("email", " ｔａｒｏ＠example.jp ", "taro@example.jp"),
        ("kana", "やまだ　ﾀﾛｳ", "ヤマダ　タロウ"),
        ("amount", "２２０，０００円", "220000"),
        ("amount", "¥ 1,200", "1200"),
    ],
)
def test_normalize(kind: str, value: str, expected: str) -> None:
    assert normalize.normalize(kind, value) == expected


class AmountForm(StyledFormMixin, forms.Form):
    base_wage = forms.IntegerField(min_value=0)
    note = forms.CharField(required=False)
    contact_phone = forms.CharField(required=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.style_fields()


def test_amount_and_phone_fields_are_normalized_when_saved() -> None:
    form = AmountForm({"base_wage": "２２０,０００", "note": " ＡＢ ", "contact_phone": "03ー1"})
    assert form.is_valid(), form.errors
    assert form.cleaned_data["base_wage"] == 220000
    assert form.cleaned_data["contact_phone"] == "03-1"
    assert form.cleaned_data["note"] == "ＡＢ"  # 対象でない欄はそのまま
    html = str(form["base_wage"])
    assert 'type="text"' in html and 'inputmode="numeric"' in html
    assert 'data-normalize="amount"' in html
    assert "data-normalize" not in str(form["note"])


def test_kinds_follow_the_field_names() -> None:
    assert normalize_kind("phone_number", forms.CharField()) == "phone"
    assert normalize_kind("postal_code", forms.CharField()) == "postal"
    assert normalize_kind("monthly_fee", forms.IntegerField()) == "amount"
    assert normalize_kind("weekly_working_days", forms.IntegerField()) == ""
    assert normalize_kind("email", forms.EmailField()) == "email"


@pytest.mark.django_db
def test_host_form_saves_the_normalized_postal_code_and_phone() -> None:
    form = HostOrganizationForm(
        {
            "entity_type": "sole_proprietor",
            "name": "（ダミー）山田製作所",
            "name_kana": "やまだせいさくしょ",
            "representative_name": "山田 太郎",
            "postal_code": "１０００００１",
            "address": "東京都千代田区",
            "phone_number": "０３－０００ー0000",
        }
    )
    assert form.is_valid(), form.errors
    assert form.cleaned_data["postal_code"] == "100-0001"
    assert form.cleaned_data["phone_number"] == "03-000-0000"
    assert form.cleaned_data["name_kana"] == "やまだせいさくしょ"  # カナは外国人の氏名だけ直す


@pytest.mark.parametrize(
    ("kind", "value", "expected"),
    [
        ("date", "20260710", "2026-07-10"),
        ("date", "2026/7/10", "2026-07-10"),
        ("date", "２０２６年７月１０日", "2026-07-10"),
        ("date", "R8.7.10", "2026-07-10"),
        ("date", "令和8年7月10日", "2026-07-10"),
        ("date", "令和元年5月1日", "2019-05-01"),
        ("date", "H31.4.30", "2019-04-30"),
        ("date", "S64.1.7", "1989-01-07"),
        ("date", "2026-02-30", "2026-02-30"),  # 無い日は直さない（日付の検査で知らせる）
        ("date", "あした", "あした"),
        ("time", "930", "09:30"),
        ("time", "0930", "09:30"),
        ("time", "9:30", "09:30"),
        ("time", "9時30分", "09:30"),
        ("time", "9時", "09:00"),
        ("time", "２３：５９", "23:59"),
        ("time", "25:00", "25:00"),
    ],
)
def test_dates_and_times(kind: str, value: str, expected: str) -> None:
    """日付と時刻の自動の整形（修正 1 回目 B16。forms.js も同じ規則）。"""
    assert normalize.normalize(kind, value) == expected


class DateTimeForm(StyledFormMixin, forms.Form):
    on = forms.DateField(widget=forms.DateInput)
    at = forms.TimeField(widget=forms.TimeInput(attrs={"type": "time"}))

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.style_fields()


def test_date_and_time_fields_accept_the_short_forms() -> None:
    form = DateTimeForm({"on": "R8.7.10", "at": "930"})
    assert form.is_valid(), form.errors
    assert form.cleaned_data["on"].isoformat() == "2026-07-10"
    assert form.cleaned_data["at"].strftime("%H:%M") == "09:30"
    date_html, time_html = str(form["on"]), str(form["at"])
    assert 'type="text"' in date_html and "data-date" in date_html
    assert 'data-normalize="date"' in date_html and "data-today=" in date_html
    assert 'type="text"' in time_html and 'list="time-options"' in time_html
    assert 'type="time"' not in time_html


def test_birth_date_is_three_selects() -> None:
    from apps.workers.forms import WorkerForm

    html = str(WorkerForm()["birth_date"])
    assert html.count("<select") == 3 and "select--date-part" in html
    assert 'name="birth_date_year"' in html
