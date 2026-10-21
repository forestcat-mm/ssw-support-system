"""雇用契約の書類（第1-6号・第1-5号・第1-4号）の生成（T34、ADR-0023）。

- template.docx は道具（tools/forms/make_contract_templates.py）の出力そのもので、タグは値が無いと
  元の文字に戻る。値を入れずに差し込んだ本文は original と同じ（第1-6号は別紙２ を除く）。
- 第1-4号は配布が .doc なので、変換した converted.docx を元にする（T34-D1）。変換の前後で頁数・
  文字・表の作りが同じこと、もう一度変換しても同じことを確かめる。
- A「様式との一致」①②: 頁数と固定の文言が空の様式と同じ。値は欄に入る。
"""

import datetime
import json
import re
import shutil
import subprocess
import sys
import zipfile
from decimal import Decimal
from pathlib import Path
from typing import Any

import docx
import pytest
from django.conf import settings
from django.utils import timezone

from apps.contracts.models import (
    ContractWageExplanation,
    EmploymentContractAllowance,
    EmploymentContractDeduction,
    EmploymentContractShift,
    EmploymentContractVersion,
    WageComparison,
    WageUnit,
)
from apps.core.models import AuditAction, AuditLog
from apps.forms_engine.models import GeneratedDocument, GenerationStatus
from apps.workers.models import StatusOfResidence
from services import (
    contracts,
    form_checks,
    form_data_contract,
    form_docx,
    form_generation,
    wage_explanations,
)
from tests.contracts.factories import add_version, make_contract, site_with_categories
from tests.core.factories import make_user, verified_client
from tests.forms_engine import doc_conversion, pdfcheck
from tests.workers.factories import add_residence, make_worker

pytestmark = pytest.mark.django_db

ROOT = Path(settings.FORM_TEMPLATES_ROOT)
MAPPINGS = Path(settings.FORM_MAPPINGS_ROOT)
TOOLS = Path(settings.FORM_TEMPLATES_ROOT).parent.parent / "tools" / "forms"
VALID_FROM = "2026-10-18"
BASIS = datetime.date(2026, 10, 18)
TAG = re.compile(r'\{\{ (\w+) or "([^"]*)" \}\}')


def folder(form_id: str) -> Path:
    return ROOT / form_id / VALID_FROM


def full_version(**overrides: Any) -> EmploymentContractVersion:
    """様式の欄をひととおり埋めた版（交代制・手当・控除を含む）。"""
    values: dict[str, Any] = {
        "signed_on": datetime.date(2026, 3, 1),
        "expected_entry_on": datetime.date(2026, 3, 25),
        "renewal_criteria": ["workload", "performance"],
        "monthly_working_minutes": 9800,
        "annual_working_minutes": 117600,
        "annual_working_days": 245,
        "regular_holiday_weekdays": ["sat", "sun"],
        "hourly_equivalent": 1347,  # 220000 × 60 ÷ 9800 = 1346.9…
        "overtime_rate_within_60h": Decimal("25"),
        "overtime_rate_over_60h": Decimal("50"),
        "night_rate": Decimal("25"),
        "leave_allowance_rate": Decimal("60"),
        "has_raise": True,
        "raise_note": "年1回（4月）",
        "insurances": ["employees_pension", "health", "employment", "workers_compensation"],
        "hiring_health_check_on": datetime.date(2026, 3, 1),
        "periodic_health_check_interval": "1年ごと",
        "treatment_contact_department": "総務部",
        "treatment_contact_person": "総務課長 窓口 一郎",
        "treatment_contact_phone": "03-0000-0100",
        "consultation_contact_department": "総務部",
        "consultation_contact_person": "総務課長 窓口 一郎",
        "consultation_contact_phone": "03-0000-0100",
        "work_rules_articles_hours": "第15条～第18条",
        "work_rules_location": "事務室に掲示",
        "has_deduction_agreement": True,  # 通信費（その他）の控除がある
    }
    contract = make_contract(make_worker(), site=site_with_categories(), **(values | overrides))
    version = contract.versions.get()
    EmploymentContractAllowance.objects.create(
        version=version, name="皆勤手当", amount=5000, calculation_method="欠勤無しの月に支給"
    )
    EmploymentContractAllowance.objects.create(
        version=version, name="固定残業手当", amount=30000, fixed_overtime_hours=Decimal("20")
    )
    EmploymentContractDeduction.objects.create(version=version, kind="tax", amount=8600)
    EmploymentContractDeduction.objects.create(
        version=version, kind="other", name="通信費", amount=1500
    )
    EmploymentContractShift.objects.create(
        version=version,
        start_time=datetime.time(13),
        end_time=datetime.time(22),
        applies_to="遅番の週",
        daily_working_minutes=480,
    )
    return version


def explain(version: EmploymentContractVersion, **overrides: Any) -> ContractWageExplanation:
    values: dict[str, Any] = {
        "comparison": WageComparison.COMPARABLE,
        "wage_unit": WageUnit.MONTHLY,
        "applicant_duties": "製造ラインの作業員",
        "applicant_experience_years": Decimal("2"),
        "japanese_duties": "製造ラインの作業員（入社3年目）",
        "japanese_age": 24,
        "japanese_sex": "male",
        "japanese_experience_years": Decimal("3"),
        "japanese_wage": 210000,
        "has_wage_rules": True,
        "rules_wage": 200000,
        "reason": "賃金規程の同じ等級で決めている",
    }
    return wage_explanations.save(version, values | overrides, by=None)


def data_for(form_id: str, version: EmploymentContractVersion, author: str = "") -> dict[str, Any]:
    if form_id == "1-6":
        return form_data_contract.build_1_6(version)
    if form_id == "1-5":
        return form_data_contract.build_1_5(version)
    return form_data_contract.build_1_4(
        version, BASIS, author=author or form_data_contract.default_author(version)
    )


def check(form_id: str, version: EmploymentContractVersion, **kwargs: Any) -> Any:
    return form_generation.check(
        form_id, target=version, data=data_for(form_id, version, **kwargs), basis_on=BASIS
    )


def codes(result: form_checks.CheckResult) -> dict[str, str]:
    return {finding.code: finding.severity for finding in result.findings}


# --- template.docx とマッピング定義（道具の出力・タグ以外を変えない）-------------------------


@pytest.mark.parametrize(
    ("form_id", "source"),
    [("1-6", "original.docx"), ("1-5", "original.docx"), ("1-4", "converted.docx")],
)
def test_templates_and_mappings_are_reproduced_by_the_tool(
    tmp_path: Path, form_id: str, source: str
) -> None:
    """template.docx とマッピング定義は道具の出力そのもの（手で直さない）。"""
    shutil.copyfile(folder(form_id) / source, tmp_path / source)
    result = subprocess.run(
        [sys.executable, str(TOOLS / "make_contract_templates.py"), form_id, str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert (tmp_path / "template.docx").read_bytes() == (
        folder(form_id) / "template.docx"
    ).read_bytes()
    assert result.stdout == (MAPPINGS / form_id / f"{VALID_FROM}.json").read_text("utf-8")


def body_texts(path: Path) -> list[str]:
    """本文の段落の文字（表の中を含む。本文の順）。"""
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    texts: list[str] = []

    def walk(element: Any) -> None:
        for child in element.iterchildren():
            tag = child.tag.removeprefix(namespace)
            if tag == "p":
                texts.append("".join(t.text or "" for t in child.iter(f"{namespace}t")))
            elif tag in ("tbl", "tr", "tc", "sdt", "sdtContent"):
                walk(child)

    walk(docx.Document(str(path)).element.body)
    return texts


@pytest.mark.parametrize(
    ("form_id", "source"),
    [("1-6", "original.docx"), ("1-5", "original.docx"), ("1-4", "converted.docx")],
)
def test_template_rendered_empty_is_the_original(tmp_path: Path, form_id: str, source: str) -> None:
    """値を入れずに差し込んだ本文は original と同じ（第1-6号は別紙２ を外す。T34-D3）。"""
    out = tmp_path / "empty.docx"
    form_docx.render(folder(form_id) / "template.docx", out, {})
    original = body_texts(folder(form_id) / source)
    if form_id == "1-6":
        cut = next(
            i for i, text in enumerate(original) if text.startswith("参考様式第１－６号　別紙２")
        )
        original = original[:cut]  # 別紙２ と、その後ろの最後の空の段落
    assert body_texts(out) == original


def test_mapping_targets_are_the_tags_of_the_template() -> None:
    for form_id in ("1-6", "1-5", "1-4"):
        with zipfile.ZipFile(folder(form_id) / "template.docx") as archive:
            tags = {
                name for name, _original in TAG.findall(archive.read("word/document.xml").decode())
            }
        mapping = json.loads((MAPPINGS / form_id / f"{VALID_FROM}.json").read_text("utf-8"))
        assert {item["target"] for item in mapping["fields"]} == tags, form_id


# --- 第1-4号の .doc の変換（T34-D1）-----------------------------------------------------------


def test_converted_1_4_has_the_same_pages_and_characters_as_the_doc() -> None:
    """(a) 頁数と空白を除いた文字が、入管の .doc を PDF にしたものと同じ。"""
    from_doc = pdfcheck.doc_to_pdf(folder("1-4") / "original.doc")
    blank = pdfcheck.blank_pdf(folder("1-4") / "converted.docx")
    assert len(pdfcheck.pdf_text(from_doc)) == len(pdfcheck.pdf_text(blank)) == 2
    squash = re.compile(r"\s+")
    assert squash.sub("", "".join(pdfcheck.pdf_text(from_doc))) == squash.sub(
        "", "".join(pdfcheck.pdf_text(blank))
    )


def _odt_tables(path: Path, workdir: Path) -> list[tuple[int, int, int]]:
    subprocess.run(
        [
            "soffice",
            "--headless",
            f"-env:UserInstallation=file://{workdir / 'profile'}",
            "--convert-to",
            "odt",
            "--outdir",
            str(workdir),
            str(path),
        ],
        capture_output=True,
        timeout=120,
        check=True,
    )
    content = zipfile.ZipFile(workdir / f"{path.stem}.odt").read("content.xml").decode()
    tables = re.findall(r"<table:table .*?</table:table>", content, re.S)
    return [
        (
            len(re.findall(r"<table:table-row", table)),
            len(re.findall(r'number-columns-spanned="[2-9]', table)),
            len(re.findall(r'number-rows-spanned="[2-9]', table)),
        )
        for table in tables
    ]


def test_converted_1_4_has_the_same_tables_as_the_doc(tmp_path: Path) -> None:
    """(b) 表の数・行の数・結合のセルの数が、.doc と converted.docx で同じ。"""
    shutil.copyfile(folder("1-4") / "original.doc", tmp_path / "original.doc")
    from_doc = _odt_tables(tmp_path / "original.doc", tmp_path)
    xml = zipfile.ZipFile(folder("1-4") / "converted.docx").read("word/document.xml").decode()
    tables = re.findall(r"<w:tbl>.*?</w:tbl>", xml, re.S)
    from_docx = [
        (
            len(re.findall(r"<w:tr[ >]", table)),
            len(re.findall(r"<w:gridSpan", table)),
            len(re.findall(r'<w:vMerge w:val="restart"', table)),
        )
        for table in tables
    ]
    assert from_doc == from_docx == [(5, 0, 0), (7, 5, 1), (7, 5, 1)]


def test_converting_1_4_again_gives_the_same_xml(tmp_path: Path) -> None:
    """(c) もう一度変換しても word/*.xml が同じ（LibreOffice の版が違えば、理由つきで飛ばす）。"""
    convert_doc = doc_conversion.convert_doc_or_skip(folder("1-4"))
    out = tmp_path / "again.docx"
    convert_doc.convert(folder("1-4") / "original.doc", out)
    with (
        zipfile.ZipFile(out) as again,
        zipfile.ZipFile(folder("1-4") / "converted.docx") as registered,
    ):
        names = [name for name in registered.namelist() if name.startswith("word/")]
        assert names and sorted(names) == sorted(
            name for name in again.namelist() if name.startswith("word/")
        )
        for name in names:
            assert again.read(name) == registered.read(name), name


# --- データ ---------------------------------------------------------------------------------


def test_contract_data_uses_full_width_numbers_and_replaces_printed_text(loaded: None) -> None:
    version = full_version(national_holidays_off=False)
    values = form_generation.field_values(
        json.loads((MAPPINGS / "1-6" / f"{VALID_FROM}.json").read_text("utf-8")),
        form_data_contract.build_1_6(version),
    )
    assert values["signed_y"] == "２０２６"
    assert values["signed_m"] == "　　３" and values["signed_d"] == "　　１"  # 欄の幅にそろえる
    assert values["start_h"] == "　８" and values["start_mi"] == "３０"
    assert values["wage_monthly"] == "■" and values["wage_hourly"] == "□"
    assert values["wage_monthly_amount"] == "２２０，０００".rjust(7, "　")
    # 「毎月◯日」は印刷済みの「□　毎月　　　日、…」の 1 つ目に入れる（修正 1 回目 B4）
    assert values["wage_closing_day"] == "■　毎月　　末日、　□　毎月　　　日"
    # 翌月払いは様式に欄が無いので、印刷済みの文を値で置き換える（T34-D4）
    assert values["wage_payment_day"] == "■　翌月２５日"
    assert form_data_contract.wage_day_text("当月20日") == "■　毎月　20日、　□　毎月　　　日"
    assert values["national_holidays"] == "　" * 8  # 国民の祝日が休日でない（T34-D8）
    assert values["renewal_limit"] == "無"
    assert str(values["check_interval"]).startswith("１年")  # 「（その後　ごとに実施）」と重ねない
    assert str(values["fixed_overtime_name"]).startswith("固定残業")  # 印刷済みの「手当」と重ねない
    assert values["fixed_overtime_hours"] == values["fixed_overtime_hours_over"]
    assert str(values["deduction_other1_name"]).startswith("通信費")
    # 220,000 + 手当（住宅 10,000・皆勤 5,000・固定残業 30,000）
    # − 控除（居住費 30,000・税 8,600・通信費 1,500）
    assert str(values["take_home"]).strip("　") == "２２４，９００"
    assert "annex2" not in values  # 別紙２ は出さない


def test_contacts_of_old_versions_go_to_the_department_field(loaded: None) -> None:
    version = full_version(
        treatment_contact_department="",
        treatment_contact_person="",
        treatment_contact_phone="",
        treatment_explanation_contact="総務部 山田（03-0000-0000）",
    )
    data = form_data_contract.build_1_6(version)
    assert data["c.treatment_department"] == "総務部 山田（03-0000-0000）"
    assert form_data_contract.legacy_contacts(version)
    assert codes(check("1-6", version)) == {"contacts_not_split": "warn"}
    # 新しい版の初期値でも、部署名の欄に写す（T34-D4）
    initial = contracts.initial_from(version)
    assert initial["treatment_contact_department"] == "総務部 山田（03-0000-0000）"
    assert "treatment_explanation_contact" not in initial


def test_signature_of_host_is_split_into_two_lines(loaded: None) -> None:
    version = full_version()
    host = version.contract.host_organization
    signature = form_data_contract.build_1_5(version)["c.host_signature"]
    assert signature.split("\n")[0] == host.name


# --- 生成前の検査 ---------------------------------------------------------------------------------


def test_complete_version_has_no_findings(loaded: None) -> None:
    version = full_version()
    explain(version)
    assert check("1-6", version).findings == []
    assert check("1-5", version).findings == []
    assert check("1-4", version).findings == []


def test_rows_beyond_the_form_are_warned(loaded: None) -> None:
    version = full_version()
    for name in ("住宅手当2", "家族手当", "通勤手当"):
        EmploymentContractAllowance.objects.create(version=version, name=name, amount=1000)
    result = check("1-6", version)
    assert codes(result) == {"rows_overflow": "warn"}
    assert len(result.warnings) == 2  # 本体の手当の名前（3）と別紙１ の行（4）
    # 合計は全部の行から（T34-D7）
    assert form_data_contract.build_1_6(version)["c.monthly_total"] == contracts.monthly_total(
        version
    )


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"hourly_equivalent": 1345}, {"hourly_equivalent_differs": "warn"}),  # 1346.9 から 1.9 円
        ({"hourly_equivalent": 1346}, {}),  # ±1 円まで
        ({"hourly_equivalent": None}, {"hourly_equivalent_missing": "warn"}),
        (
            {"monthly_working_minutes": 10400, "hourly_equivalent": 1269},
            {"monthly_hours_not_average": "warn"},
        ),
        ({"monthly_working_minutes": None}, {"working_hours_missing": "warn"}),
        ({"signed_on": None}, {"required": "warn"}),  # 冒頭の日付は空欄（T34-D5）
    ],
)
def test_wage_and_hours_rules(
    loaded: None, overrides: dict[str, Any], expected: dict[str, str]
) -> None:
    """換算額は労基則 第19条第1項第4号（月給 ÷ 月の所定労働時間数。ADR-0023）。"""
    assert codes(check("1-6", full_version(**overrides))) == expected


@pytest.mark.parametrize(
    ("hourly", "expected"), [(1250, {}), (1300, {"hourly_equivalent_differs": "warn"})]
)
def test_daily_wage_is_divided_by_daily_hours(
    loaded: None, hourly: int, expected: dict[str, str]
) -> None:
    """日給は 1 日の所定労働時間数で割る（第19条第1項第2号。10,000 円 ÷ 8 時間）。"""
    version = full_version(
        wage_type="daily", base_wage=10000, hourly_equivalent=hourly, monthly_equivalent=204167
    )
    assert codes(check("1-6", version)) == expected


def test_version_not_in_force_on_the_basis_date_is_warned(loaded: None) -> None:
    version = full_version()
    newer = add_version(version.contract, changed_on=datetime.date(2026, 10, 1), base_wage=230000)
    result = check("1-5", version)
    assert codes(result) == {"version_not_in_force": "warn"}
    assert f"version {newer.version}" in result.warnings[0].message or "第2版" in str(
        result.warnings[0].message
    )


def test_conversion_annex_is_warned(loaded: None) -> None:
    version = full_version(
        ends_on=datetime.date(2031, 3, 31),
        indefinite_conversion=True,
        conversion_conditions_change="changed",
    )
    assert codes(check("1-6", version)) == {"conversion_annex": "warn"}
    data = form_data_contract.build_1_6(version)
    assert data["c.conversion_y"] == 2031 and data["c.conversion_m"] == 4
    assert data["c.conversion_change"] == " 有（別紙２のとおり）"


def test_wage_explanation_missing_blocks_form_1_4(loaded: None) -> None:
    version = full_version()
    assert codes(check("1-4", version)) == {"wage_basis_missing": "block"}
    explain(version, comparison=WageComparison.NONE)
    assert codes(check("1-4", version)) == {"wage_basis_no_japanese": "block"}
    explain(version, comparison=WageComparison.CLOSEST, reason="")
    result = check("1-4", version)
    assert codes(result) == {"wage_basis_item": "block"}
    destination = result.blocking[0].destination
    assert destination is not None and destination.url().endswith(
        f"/contracts/{version.contract_id}/versions/1/wage-explanation/"
    )


def test_author_is_required_for_form_1_4(loaded: None) -> None:
    version = full_version()
    explain(version)
    version.contract.host_organization.representative_name = ""
    version.contract.host_organization.representative_title = ""
    version.contract.host_organization.save()
    assert codes(check("1-4", version)) == {"required": "block"}
    assert codes(check("1-4", version, author="人事部長 作成 花子")) == {}


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"japanese_wage": 230000}, ["wage_below_japanese"]),
        ({"rules_wage": 225000}, ["wage_below_rules"]),
        ({"japanese_wage": 220000, "rules_wage": 220000}, []),  # 同等は可
        # 時間給にそろえると、月給の版は 1 時間当たりの金額（換算額）で比べる
        (
            {"wage_unit": WageUnit.HOURLY, "japanese_wage": 1400, "rules_wage": 1300},
            ["wage_below_japanese"],
        ),
        # ３（最も近い職務の日本人）は報酬が大きく異なる日本人と比べる欄（記載例も高い報酬）
        ({"comparison": WageComparison.CLOSEST, "japanese_wage": 350000, "rules_wage": 350000}, []),
    ],
)
def test_wage_lower_than_japanese_is_a_strong_warning(
    loaded: None, overrides: dict[str, Any], expected: list[str]
) -> None:
    """基準省令 第1条第1項第3号（日本人と同等以上）。役職・経験の違いはシステムで判断しない。"""
    version = full_version()
    explain(version, **overrides)
    result = check("1-4", version)
    assert [finding.code for finding in result.warnings] == expected
    assert all(finding.strong for finding in result.warnings)
    assert result.blocking == []


# --- 報酬の説明（版と 1 対 1。直せる。新しい版に写す。差分・届出に関わらない）-----------------


def test_explanation_is_copied_to_the_new_version_and_not_in_the_diff(loaded: None) -> None:
    version = full_version()
    explain(version)
    values = contracts.initial_from(version)
    categories = values.pop("categories")
    values.pop("site")
    new = EmploymentContractVersion(
        **(
            values
            | {
                "changed_on": datetime.date(2026, 9, 1),
                "change_reason": "窓口の変更",
                "treatment_contact_person": "総務課長 窓口 二郎",
            }
        ),
        site=version.site,
    )
    rows = contracts.detail_initial(version)
    saved = contracts.create_version(
        new,
        contract=version.contract,
        categories=categories,
        details=contracts.Details(
            shifts=[EmploymentContractShift(**row) for row in rows["shifts"]],
            allowances=[EmploymentContractAllowance(**row) for row in rows["allowances"]],
            deductions=[EmploymentContractDeduction(**row) for row in rows["deductions"]],
        ),
        by=None,
    )
    copied = wage_explanations.explanation_of(saved.version)
    assert copied is not None and copied.pk != wage_explanations.explanation_of(version).pk  # type: ignore[union-attr]
    assert copied.japanese_wage == 210000
    assert "wage_explanation" not in contracts.diff(version, saved.version)
    # 窓口の変更（雇用条件に影響しない項目）では届出を起票しない
    assert saved.filing is None


def test_explanation_screen_saves_and_records_the_audit_log(loaded: None) -> None:
    version = full_version(
        comparable_japanese_note="同職位の日本人", comparable_japanese_wage=215000
    )
    worker = version.contract.worker
    client = verified_client(make_user("tanto34", roles=("support_staff",)))
    url = f"/workers/{worker.pk}/contracts/{version.contract_id}/versions/1/wage-explanation/"
    page = client.get(url)
    assert page.status_code == 200
    form = page.context["form"]
    assert form.initial["japanese_duties"] == "同職位の日本人"  # 版の「同等報酬」から
    post = {
        page.context["once_field"]: page.context["once_token"],
        "comparison": "comparable",
        "wage_unit": "monthly",
        "applicant_duties": "製造ラインの作業員",
        "applicant_experience_years": "2",
        "japanese_duties": "同職位の日本人",
        "japanese_age": "24",
        "japanese_sex": "female",
        "japanese_experience_years": "3",
        "japanese_wage": "215000",
        "has_wage_rules": "false",
        "reason": "同じ基準で決めている",
    }
    response = client.post(url, post)
    assert response.status_code == 302
    explanation = wage_explanations.explanation_of(version)
    assert explanation is not None and explanation.has_wage_rules is False
    assert AuditLog.objects.filter(
        action=AuditAction.CREATE, content_type__model="contractwageexplanation"
    ).exists()
    # 版を作った後でも直せる（更新も監査ログに残る）
    page = client.get(url)
    client.post(
        url, post | {page.context["once_field"]: page.context["once_token"], "japanese_age": "25"}
    )
    explanation.refresh_from_db()
    assert explanation.japanese_age == 25
    assert AuditLog.objects.filter(
        action=AuditAction.UPDATE, content_type__model="contractwageexplanation"
    ).exists()


def test_fill_in_from_the_register_and_the_contract(loaded: None) -> None:
    """「台帳と契約から入れる」（修正 1 回目 B12）: 職務内容・報酬の単位・経験年数の目安。

    保存はしない。
    """
    version = full_version()
    worker = version.contract.worker
    filled = wage_explanations.from_records(version)
    assert filled.values["applicant_duties"] == version.duties
    assert filled.values["wage_unit"] == "monthly"
    assert "applicant_experience_years" not in filled.values and filled.experience_from == ""
    add_residence(worker, status_of_residence=StatusOfResidence.TECHNICAL_INTERN_2)
    filled = wage_explanations.from_records(version)
    assert filled.values["applicant_experience_years"] == Decimal(3)
    third = add_residence(worker, status_of_residence=StatusOfResidence.TECHNICAL_INTERN_3)
    assert wage_explanations.from_records(version).values["applicant_experience_years"] == 5
    third.voided_at = timezone.now()  # 取り消した行は使わない
    third.save()
    assert wage_explanations.from_records(version).values["applicant_experience_years"] == 3
    # 画面: 入れた値で出し直し、ほかの欄の入力は残す。保存はしない
    client = verified_client(make_user("tanto34", roles=("support_staff",)))
    url = f"/workers/{worker.pk}/contracts/{version.contract_id}/versions/1/wage-explanation/"
    page = client.get(url)
    response = client.post(
        url,
        {
            page.context["once_field"]: page.context["once_token"],
            "action": "fill",
            "comparison": "comparable",
            "japanese_age": "24",
        },
    )
    assert response.status_code == 200
    form = response.context["form"]
    assert form.initial["applicant_duties"] == version.duties
    assert form.initial["applicant_experience_years"] == "3"
    assert form.initial["comparison"] == "comparable" and form.initial["japanese_age"] == "24"
    assert 'id="wage-explanation-filled"' in response.content.decode()
    assert wage_explanations.explanation_of(version) is None


# --- 生成（A の①②）---------------------------------------------------------------------------


def printed_and_written(form_id: str, data: dict[str, Any]) -> tuple[list[str], list[str]]:
    """書いた値と、その欄の印刷済みの文字（空の様式の文字から除く分）。"""
    with zipfile.ZipFile(folder(form_id) / "template.docx") as archive:
        originals = dict(TAG.findall(archive.read("word/document.xml").decode()))
    mapping = json.loads((MAPPINGS / form_id / f"{VALID_FROM}.json").read_text("utf-8"))
    values = form_generation.field_values(mapping, data)
    written = [str(value) for value in values.values()]
    printed = [originals[target] for target in values]
    return written, printed


@pytest.mark.parametrize(("form_id", "pages"), [("1-6", 4), ("1-5", 1), ("1-4", 2)])
def test_generated_contract_documents_match_the_blank_form(
    loaded: None, form_id: str, pages: int
) -> None:
    version = full_version()
    explain(version)
    data = data_for(form_id, version)
    document = form_generation.request(form_id, target=version, data=data, basis_on=BASIS, by=None)
    document.refresh_from_db()
    assert document.status == GenerationStatus.COMPLETED, document.failure_reason
    assert document.worker == version.contract.worker
    assert document.template.requires_signed_copy is (form_id != "1-4")  # T34-D9

    # ① 頁数と固定の文言（空の様式は template に何も入れずに差し込んだもの。T34-D3）
    blank = pdfcheck.blank_from_template(folder(form_id) / "template.docx")
    assert document.page_count == pages == len(pdfcheck.pdf_text(blank))
    written, printed = printed_and_written(form_id, data)
    assert pdfcheck.fixed_characters_match(Path(document.pdf_file.path), blank, written, printed)

    # ② 差込の位置の本文（書いた値が本文にある）
    text = "\n".join(body_texts(Path(document.filled_file.path)))
    for value in written:
        for line in value.split("\n"):
            assert line in text, line


def test_generation_from_the_contract_tab(loaded: None) -> None:
    version = full_version()
    explain(version)
    worker = version.contract.worker
    client = verified_client(make_user("tanto35", roles=("support_staff",)))
    tab = client.get(f"/workers/{worker.pk}/contracts/").content.decode()
    assert 'id="contract-generate"' in tab and 'id="wage-explanation-edit"' in tab
    url = f"/workers/{worker.pk}/contracts/{version.contract_id}/versions/1/generate/1-4/"
    page = client.get(url)
    assert page.status_code == 200 and "不足はありません" in page.content.decode()
    assert page.context["form"].initial["author"] == form_data_contract.default_author(version)
    response = client.post(
        url,
        {
            page.context["once_field"]: page.context["once_token"],
            "basis_on": BASIS.isoformat(),
            "author": "人事部長 作成 花子",
            "action": "generate",
        },
    )
    document = GeneratedDocument.objects.get(worker=worker)
    assert response["Location"] == f"/forms/documents/{document.pk}/"
    assert document.target == version and document.template.form_id == "1-4"
    # 作成責任者を変えた値はスナップショットに残る（T37-D10 と同じ）
    snapshot = json.loads(document.snapshot)
    assert snapshot["data"]["w.author"] == "人事部長 作成 花子"
    content = client.get(response["Location"]).content.decode()
    assert f"?contract={version.contract_id}&amp;version=1" in content
    assert client.get(url.replace("1-4", "9-9")).status_code == 404


# --- ダミーデータ -----------------------------------------------------------------------------


def test_dummy_contracts_follow_article_19_and_have_wage_explanations(seeded: list[Any]) -> None:
    """ダミーの版は換算額が第19条の計算に合い、報酬の説明があり、強い警告が出ない（ADR-0023）。"""
    unexpected = {
        "hourly_equivalent_differs",
        "hourly_equivalent_missing",
        "monthly_hours_not_average",
        "working_hours_missing",
        "contacts_not_split",
        "wage_basis_missing",
        "wage_basis_item",
        "wage_basis_conversion",
        "wage_below_japanese",
        "wage_below_rules",
    }
    versions = EmploymentContractVersion.objects.filter(contract__worker__in=seeded)
    assert versions.count() == 32 + 5  # 32 契約（31 番は T39）。昇給 3・減給 2 の 2 版目
    for version in versions:
        assert wage_explanations.explanation_of(version) is not None
        for form_id in ("1-6", "1-4"):
            result = form_generation.check(
                form_id,
                target=version,
                data=data_for(form_id, version),
                basis_on=version.changed_on,
            )
            assert not {finding.code for finding in result.findings} & unexpected, (
                version,
                form_id,
                [finding.message for finding in result.findings],
            )


def test_deduction_agreement_and_work_rules_location_are_checked(loaded: None) -> None:
    """第1-6号: 税金・社会保険料・雇用保険料のほかの控除があるのに労使協定が「無」、就業規則を確認
    できる場所や方法が空のときは警告（2026-10-20）。"""
    version = full_version(has_deduction_agreement=False, work_rules_location="")
    EmploymentContractDeduction.objects.create(
        version=version, kind="housing", name="社宅費", amount=20000
    )
    found = codes(check("1-6", version))
    assert found["deduction_without_agreement"] == "warn"
    assert found["work_rules_location_missing"] == "warn"
    # 協定が「有」で場所を入れた版は出ない（test_complete_version_has_no_findings の full_version）
