"""段階3の受け入れ確認（T40 の A。make gate3 のときだけ動く。通常の make e2e では飛ばす）。

seed_dummy と seed_documents（T39）を流し、担当者（dummy_tanto）が画面だけで次を通す。管理画面
（/admin/）には 1 度も行かない。
1. 署名済み書類の回収のタスク → 帳票タブ → 署名済みのスキャンを添付 → 署名済み・タスク完了。
2. 支援計画書（第1-17号）を 2 回生成（同じ内容でも別の生成履歴）。Excel の構造は公式のブックと同じ。
3. 事前ガイダンスの確認書を日中併記の版で生成（25 番。中国語）。PDF にパスワード・印刷禁止が無い。
4. 警告付きの生成（15 番の第13-1号。警告を確かめる印が要る）。生成履歴に警告が残る。
5. 加入証明なしの生成不可（31 番の第3-1号。「加入の証明書が添付されていません」）→ 不足の
   「直す」から受入れ機関の画面へ。申請の書類の一覧で加入の証明書の行が未添付。

- 基準日は今日。GATE_DATE=YYYY-MM-DD で変えられる（gate2 と同じく、アプリの「今」をずらす）。
- 操作の時間は参考として出すだけ（手間の測定は人が行う。docs/tasks-phase3.md の A）。
"""

import json
import os
import time
from collections.abc import Iterator
from datetime import date, datetime
from io import StringIO
from pathlib import Path
from urllib.parse import urlparse

import pytest
from django.core.management import call_command
from django.utils import timezone
from playwright.sync_api import Page, Request, expect
from pypdf import PdfReader
from pytest_django.fixtures import Settings
from pytest_django.live_server_helper import LiveServer

from apps.core.models import User
from apps.filings.models import Filing, FilingKind, FilingStatus
from apps.forms_engine.models import GeneratedDocument, GenerationStatus
from apps.seed import document_builders
from apps.seed.context import DUMMY_USERNAME
from apps.seed.models import SeedObject
from apps.support.models import Briefing, SupportPlan
from apps.workers.models import ForeignWorker
from e2e.conftest import SignIn
from services import contracts
from services.support_catalog import SupportItem
from tests.conftest import PDF_BYTES
from tests.core.factories import TEST_PASSWORD
from tests.forms_engine.test_files import ORIGINAL_1_17, structure

pytestmark = pytest.mark.skipif(os.environ.get("GATE3") != "1", reason="make gate3 のときだけ動く")

GUIDANCE_BILINGUAL = 25  # 入国前・中国語・完全な事前ガイダンス（seed の GUIDANCE_COMPLETE）
WITH_WARNINGS = 15
UNCERTIFIED = 31
PLAN_TWICE = 3


@pytest.fixture
def base_date(monkeypatch: pytest.MonkeyPatch) -> date:
    raw = os.environ.get("GATE_DATE", "")
    if not raw:
        return timezone.localdate()
    target = date.fromisoformat(raw)
    shift = target - timezone.localdate()
    real_now = timezone.now

    def shifted() -> datetime:
        return real_now() + shift

    monkeypatch.setattr(timezone, "now", shifted)
    return target


@pytest.fixture
def seeded(settings: Settings, tmp_path: Path, base_date: date) -> list[ForeignWorker]:
    """seed_dummy の 31 人と、seed_documents の生成物の場面。"""
    settings.DEBUG = True
    settings.ALLOW_DUMMY_DATA = True
    settings.STORAGES = {
        **settings.STORAGES,
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path / "media")},
        },
    }
    call_command("load_form_templates", stdout=StringIO())
    call_command("seed_dummy", "--date", base_date.isoformat(), stdout=StringIO())
    call_command("seed_documents", stdout=StringIO())
    user = User.objects.get(username=DUMMY_USERNAME)
    user.set_password(TEST_PASSWORD)
    user.save(update_fields=["password"])
    ids = SeedObject.objects.filter(content_type__model="foreignworker").values("object_id")
    return list(ForeignWorker.objects.filter(pk__in=ids).order_by("pk"))


@pytest.fixture
def admin_requests(page: Page) -> Iterator[list[str]]:
    seen: list[str] = []

    def watch(request: Request) -> None:
        if urlparse(request.url).path.startswith("/admin/"):
            seen.append(request.url)

    page.on("request", watch)
    yield seen


def generated(page: Page) -> GeneratedDocument:
    expect(page.locator("#document-status")).to_have_text("生成しました。")
    document_id = int(urlparse(page.url).path.rstrip("/").split("/")[-1])
    return GeneratedDocument.objects.select_related("template").get(pk=document_id)


def download_pdf(page: Page, tmp_path: Path) -> Path:
    with page.expect_download() as info:
        page.locator("#document-pdf").click()
    path: Path = tmp_path / str(info.value.suggested_filename)
    info.value.save_as(path)
    return path


def test_phase3_gate(
    page: Page,
    live_server: LiveServer,
    sign_in: SignIn,
    seeded: list[ForeignWorker],
    base_date: date,
    admin_requests: list[str],
    tmp_path: Path,
) -> None:
    page.set_viewport_size({"width": 1366, "height": 900})
    worker = {number: seeded[number - 1] for number in range(1, len(seeded) + 1)}
    assert len(seeded) == 31
    sign_in(User.objects.get(username=DUMMY_USERNAME))
    base = live_server.url
    started = time.monotonic()

    # 1. 回収のタスク → 帳票タブ → 署名済みのスキャン → 署名済み・タスク完了
    awaiting = GeneratedDocument.objects.get(
        worker=worker[document_builders.COLLECTION], template__form_id="1-6"
    )
    task = Filing.objects.get(
        kind=FilingKind.SIGNED_COPY_COLLECTION,
        object_id=awaiting.pk,
        status=FilingStatus.NOT_STARTED,
    )
    page.goto(f"{base}/filings/{task.pk}/")
    page.locator("#open-documents").click()
    row = page.locator(f'#generated-documents tr[data-document="{awaiting.pk}"]')
    expect(row).to_have_attribute("data-state", "awaiting")
    row.get_by_role("link", name="署名済みの書類を添付").click()
    scan = tmp_path / "signed.pdf"
    scan.write_bytes(PDF_BYTES)
    page.get_by_label("署名済みのスキャン").set_input_files(str(scan))
    page.get_by_label("署名日").fill(base_date.isoformat())
    page.get_by_role("button", name="保存する").click()
    expect(page.get_by_text("署名済みの書類を添付しました。")).to_be_visible()
    expect(
        page.locator(f'#generated-documents tr[data-document="{awaiting.pk}"]')
    ).to_have_attribute("data-state", "signed")
    task.refresh_from_db()
    assert task.status == FilingStatus.COMPLETED

    # 2. 支援計画書を 2 回生成（同じ内容でも別の生成履歴。Excel の構造は公式のブックと同じ）
    plan = SupportPlan.objects.filter(worker=worker[PLAN_TWICE]).order_by("-pk").first()
    assert plan is not None
    documents = []
    for _ in range(2):
        page.goto(f"{base}/workers/{worker[PLAN_TWICE].pk}/support/")
        page.locator("#plan-generate").click()
        page.locator("#id_propriety").check()
        page.get_by_role("button", name="検査する").click()
        expect(page.locator("#generation-findings")).to_contain_text("不足はありません")
        page.get_by_role("button", name="生成する").click()
        documents.append(generated(page))
    first, second = documents
    assert first.pk != second.pk and first.status == second.status == GenerationStatus.COMPLETED
    assert json.loads(first.snapshot)["data"] == json.loads(second.snapshot)["data"]
    assert first.template_id == second.template_id and first.created_by is not None
    expect(page.locator("#document-fields")).to_contain_text("第1版")
    assert structure(Path(second.filled_file.path)) == structure(ORIGINAL_1_17)  # 制約3
    assert not PdfReader(str(download_pdf(page, tmp_path))).is_encrypted

    # 3. 日中併記の確認書（25 番。中国語）
    briefing = Briefing.objects.get(
        record__worker=worker[GUIDANCE_BILINGUAL], record__kind=SupportItem.PRE_GUIDANCE
    )
    page.goto(f"{base}/workers/{briefing.record.worker_id}/support/briefings/{briefing.pk}/")
    page.locator("#briefing-generate").click()
    expect(page.locator("#id_language")).to_have_value("ja-zh")
    page.get_by_role("button", name="生成する").click()
    bilingual = generated(page)
    assert bilingual.template.language == "ja-zh"
    expect(page.locator("#document-language")).to_have_attribute("data-language", "ja-zh")
    pdf = PdfReader(str(download_pdf(page, tmp_path)))
    assert not pdf.is_encrypted and len(pdf.pages) == bilingual.page_count == 2

    # 4. 警告付きの生成（15 番の第13-1号）
    contract = contracts.contract_on(worker[WITH_WARNINGS], base_date)
    assert contract is not None
    page.goto(f"{base}/workers/{contract.worker_id}/contracts/?contract={contract.pk}")
    page.locator("#contract-application-generate").get_by_role(
        "link", name="第13-1号を作る"
    ).click()
    expect(page.locator("#generation-findings")).to_contain_text("キャリアアッププラン")
    page.get_by_role("button", name="生成する").click()
    expect(page.locator("#generate-form")).to_contain_text("警告を確かめてから生成してください")
    page.locator("#id_accept_warnings").check()
    page.get_by_role("button", name="生成する").click()
    warned = generated(page)
    expect(page.locator("#document-findings")).to_contain_text("キャリアアッププラン")
    assert [finding["code"] for finding in warned.findings] == ["career_plan_missing"]

    # 5. 加入証明なしの生成不可（31 番の第3-1号）→「直す」で受入れ機関の画面へ
    uncertified = worker[UNCERTIFIED]
    page.goto(f"{base}/workers/{uncertified.pk}/documents/application/")
    expect(page.locator('#application-set-rows [data-row="coe_ind-3-1"]')).to_have_attribute(
        "data-status", "missing"
    )
    before = GeneratedDocument.objects.count()
    contract = contracts.contract_on(uncertified, base_date)
    assert contract is not None
    page.goto(f"{base}/workers/{uncertified.pk}/contracts/?contract={contract.pk}")
    page.locator("#contract-application-generate").get_by_role("link", name="第3-1号を作る").click()
    findings = page.locator("#generation-findings")
    expect(findings).to_contain_text("生成できません")
    expect(findings).to_contain_text("加入の証明書（")
    expect(page.get_by_role("button", name="生成する")).to_have_count(0)
    findings.get_by_role("link", name="直す").first.click()
    expect(page).to_have_url(f"{base}/hosts/{contract.host_organization_id}/")
    assert GeneratedDocument.objects.count() == before

    print(f"\n[gate3] {time.monotonic() - started:.1f} 秒（参考。合否には使わない）")
    assert admin_requests == []
