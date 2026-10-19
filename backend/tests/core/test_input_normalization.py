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
