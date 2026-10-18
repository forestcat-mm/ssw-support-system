"""事前ガイダンス・生活オリエンテーションの確認書（第5-9号・第5-8号）の生成（T32、ADR-0021）。

- 確認書の欄は実施日時の 3 行・所属機関名・説明者の氏名だけ。欄の空白を値で置き換える。
- 固定の文が述べる事実（説明を受けた・保証金等の契約をしていない）と記録が食い違えば生成不可。
  固定の文が述べない事実（時間・費用の合意・確認書に無い欄）は警告。
- A「様式との一致」①②: 頁数と固定の文言が空の様式と同じ。差込の位置の本文に値が入る。
"""

import datetime
from pathlib import Path
from typing import Any

import docx
import pytest
from django.conf import settings

from apps.forms_engine.models import GeneratedDocument, GenerationStatus
from apps.hosts.models import SupportContract
from apps.support.models import Briefing, BriefingSession
from services import form_checks, form_data_briefing, form_generation
from services.briefings import SessionInput
from tests.contracts.factories import make_contract, site_with_categories
from tests.core.factories import make_organization, make_staff, make_user, verified_client
from tests.forms_engine import pdfcheck
from tests.support.test_briefings import all_checked, create, guidance, orientation
from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db

ROOT = Path(settings.FORM_TEMPLATES_ROOT)


@pytest.fixture
def worker(loaded: None) -> Any:
    make_organization(name="（ダミー）サンプル登録支援機関")
    worker = make_worker(assigned_staff=make_staff(name="佐藤 花子", position="支援担当"))
    contract = make_contract(worker, site=site_with_categories())
    SupportContract.objects.create(
        host_organization=contract.host_organization,
        concluded_on=datetime.date(2026, 1, 1),
        starts_on=datetime.date(2026, 1, 1),
    )
    return worker


def check(briefing: Briefing, form_id: str) -> form_checks.CheckResult:
    return form_generation.check(
        form_id,
        target=briefing,
        data=form_data_briefing.build(briefing),
        basis_on=briefing.record.occurred_on,
    )


def codes(result: Any) -> dict[str, str]:
    return {finding.code: finding.severity for finding in result.findings}


# --- データ ----------------------------------------------------------------------------


def test_slots_are_full_width_and_aligned() -> None:
    assert form_data_briefing.digits(2026, 4) == "２０２６"
    assert form_data_briefing.digits(8, 2) == "　８"
    assert form_data_briefing.digits(0, 2, zero_pad=True) == "００"
    assert form_data_briefing.session_values(3, None)["session3.year"] == "　" * 4


def test_organization_name_follows_note_3(worker: Any) -> None:
    """★3: 全部委託なら登録支援機関（自機関）、そうでなければ特定技能所属機関の名称。"""
    briefing = create(worker, orientation(worker))
    assert form_data_briefing.organization_name(briefing) == "（ダミー）サンプル登録支援機関"
    SupportContract.objects.all().delete()
    contract_host = worker.employment_contracts.get().host_organization
    assert form_data_briefing.organization_name(briefing) == contract_host.name


def test_explainer_is_the_staff_of_the_record(worker: Any) -> None:
    other = make_staff(name="鈴木 次郎", position="支援担当")
    briefing = create(worker, orientation(worker, staff=other))
    assert form_data_briefing.build(briefing)["briefing.explainer_name"] == "鈴木 次郎"


# --- 固定の文と記録の食い違い -----------------------------------------------------------------


def test_complete_records_have_no_findings(worker: Any) -> None:
    assert check(create(worker, guidance(worker)), "5-9").findings == []
    assert check(create(worker, orientation(worker)), "5-8").findings == []


@pytest.mark.parametrize(
    ("form_id", "make", "overrides", "expected"),
    [
        # 番号のある項目の細目が未チェック → 「説明を受けた」と食い違う
        ("5-9", "guidance", {"skip": ("1.b.1",)}, {"items_not_explained": "block"}),
        ("5-8", "orientation", {"skip": ("4.a.1",)}, {"items_not_explained": "block"}),
        # 確認書に無い欄（1.j）の未チェックは、固定の文が述べない → 警告
        ("5-9", "guidance", {"skip": ("1.j.1",)}, {"items_off_form_missing": "warn"}),
        # 保証金等の契約が有 → 第5-9号の 4 と末尾の文と食い違う
        (
            "5-9",
            "guidance",
            {
                "deposit_contract": "exists",
                "deposit_collector": "送出機関",
                "deposit_detail": "保証金 10 万円",
            },
            {"deposit_contract_exists": "block"},
        ),
        # 費用の合意が無い → 項目 5 は「合意している必要があること」の説明で、合意を述べない → 警告
        ("5-9", "guidance", {"fees_agreed": False}, {"fees_not_agreed": "warn"}),
    ],
)
def test_contradictions_block_and_other_facts_warn(
    worker: Any, form_id: str, make: str, overrides: dict[str, Any], expected: dict[str, str]
) -> None:
    skip = overrides.pop("skip", ())
    kind_input = guidance if make == "guidance" else orientation
    data = kind_input(worker, **overrides)
    if skip:
        data.checks = all_checked(data.kind, skip=skip)
    result = check(create(worker, data), form_id)
    assert codes(result) == expected


def test_guidance_shorter_than_the_guideline_is_a_warning(worker: Any) -> None:
    short = guidance(
        worker,
        sessions=[SessionInput(datetime.date(2026, 9, 1), datetime.time(13), datetime.time(15))],
    )
    assert codes(check(create(worker, short), "5-9")) == {"below_guideline": "warn"}


# --- 生成（A の①②）---------------------------------------------------------------------------


@pytest.mark.parametrize(("form_id", "make"), [("5-9", "guidance"), ("5-8", "orientation")])
def test_generated_confirmation_matches_the_blank_form(
    worker: Any, form_id: str, make: str
) -> None:
    kind_input = guidance if make == "guidance" else orientation
    briefing = create(worker, kind_input(worker))
    data = form_data_briefing.build(briefing)
    document = form_generation.request(
        form_id,
        target=briefing,
        data=data,
        basis_on=briefing.record.occurred_on,
        by=None,
    )
    document.refresh_from_db()
    assert document.status == GenerationStatus.COMPLETED, document.failure_reason
    assert document.worker == worker

    # ① 頁数と固定の文言（差し込んだ値を除くと、空の様式と同じ。日時の欄の空白は数えない）
    pdf = Path(document.pdf_file.path)
    blank = pdfcheck.blank_pdf(ROOT / form_id / "2026-10-17" / "original.docx")
    assert document.page_count == 1 == len(pdfcheck.pdf_text(blank))
    # 「１」など短い値が項目の番号と重なるので、文字ごとの数で比べる（欄の空白は数えない）
    assert pdfcheck.fixed_characters_match(pdf, blank, data.values(), [])

    # ② 差込の位置の本文
    paragraphs = [p.text for p in docx.Document(document.filled_file.path).paragraphs]
    first = BriefingSession.objects.filter(briefing=briefing).order_by("occurred_on").first()
    assert first is not None
    year = form_data_briefing.digits(first.occurred_on.year, 4)
    assert any(text.strip().startswith(f"{year}年") for text in paragraphs)
    assert any(text.strip().startswith("（ダミー）サンプル登録支援機関") for text in paragraphs)
    assert any(text.strip().startswith("佐藤 花子") for text in paragraphs)
    # 署名の行は手で書く（空のまま）
    signature = next(text for text in paragraphs if "特定技能外国人の署名" in text)
    assert signature.replace("　", "").replace(" ", "") == "特定技能外国人の署名年月日"


def test_generation_screen_from_the_record(worker: Any) -> None:
    briefing = create(worker, orientation(worker))
    client = verified_client(make_user("tanto7", roles=("support_staff",)))
    detail = client.get(f"/workers/{worker.pk}/support/briefings/{briefing.pk}/")
    assert 'id="briefing-generate"' in detail.content.decode()
    url = f"/workers/{worker.pk}/support/briefings/{briefing.pk}/generate/"
    page = client.get(url)
    assert page.status_code == 200 and "不足はありません" in page.content.decode()
    response = client.post(url, {page.context["once_field"]: page.context["once_token"]})
    document = GeneratedDocument.objects.get(worker=worker)
    assert (
        response.status_code == 302 and response["Location"] == f"/forms/documents/{document.pk}/"
    )
    assert document.template.form_id == "5-8" and document.target == briefing
    content = client.get(response["Location"]).content.decode()
    assert f'href="/workers/{worker.pk}/support/briefings/{briefing.pk}/"' in content
    assert document.basis_on == briefing.record.occurred_on  # 基準日は記録の実施日
