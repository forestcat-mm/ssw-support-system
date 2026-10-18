"""通しのテスト: 支援計画の版から支援計画書（第1-17号）を生成する（T31、ADR-0020）。

支援タブの「支援計画書を作る」→ 生成前の検査（警告）→ 申告と「警告を確かめた」→ 生成 →
生成物の画面で PDF をダウンロード → 生成物から写しの交付記録を作る。admin は開かない。
"""

from datetime import timedelta
from urllib.parse import urlparse

import pytest
from django.core.files.base import ContentFile
from django.utils import timezone
from playwright.sync_api import Page, Request, expect

from apps.core.models import AttachmentKind, User
from apps.hosts.models import HostMembership, SupportContract
from apps.workers.models import ForeignWorker
from e2e.conftest import SignIn
from services import attachments, form_templates, support_plans
from tests.conftest import PDF_BYTES
from tests.contracts.factories import make_contract, site_with_categories
from tests.core.factories import make_office, make_organization, make_staff
from tests.workers.factories import add_residence, make_worker


@pytest.fixture
def worker() -> ForeignWorker:
    form_templates.load_all()
    today = timezone.localdate()
    make_organization(name_kana="さんぷるとうろくしえんきかん", support_start_planned_on=today)
    office = make_office()
    make_staff(office=office, name="山田 一郎", position="支援責任者", is_support_manager=True)
    staff = make_staff(office=office, name="佐藤 花子", position="支援担当")
    worker = make_worker(assigned_staff=staff)
    add_residence(worker, expires_on=today + timedelta(days=400))
    site = site_with_categories()
    make_contract(worker, site=site)
    host = site.host_organization
    membership = HostMembership.objects.create(
        host_organization=host, field=site.field, joined_on=today - timedelta(days=300)
    )
    attachments.create_attachment(
        target=membership,
        kind=AttachmentKind.MEMBERSHIP_CERTIFICATE,
        file=ContentFile(PDF_BYTES, name="certificate.pdf"),
        created_by=None,
    )
    SupportContract.objects.create(
        host_organization=host, concluded_on=today - timedelta(days=60), starts_on=today
    )
    support_plans.create_plan(worker, host, applies_from=today, by=None)
    return worker


@pytest.mark.parametrize("width", [1366, 390])
def test_generate_the_support_plan_and_record_the_copy(
    page: Page, sign_in: SignIn, support_staff: User, worker: ForeignWorker, width: int
) -> None:
    admin_requests: list[str] = []

    def watch(request: Request) -> None:
        if urlparse(request.url).path.startswith("/admin/"):
            admin_requests.append(request.url)

    page.on("request", watch)
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(support_staff)
    base = page.url.rstrip("/")
    page.goto(f"{base}/workers/{worker.pk}/support/")
    page.locator("#plan-generate").click()

    expect(page.get_by_role("heading", level=1)).to_have_text("支援計画書を作る（第1-17号）")
    findings = page.locator("#generation-findings")
    expect(findings).to_contain_text("警告付きで生成できます")
    expect(findings).to_contain_text("「支援の適正性を確保していることの有無」の申告が無い")
    assert page.evaluate("document.documentElement.scrollWidth") <= width

    # 申告すると、その警告は消える。標準の計画の長い実施予定の文は、6pt でも収まらない警告が残る
    page.locator("#id_propriety").check()
    page.get_by_role("button", name="検査する").click()
    findings = page.locator("#generation-findings")
    expect(findings).not_to_contain_text("申告が無い")
    expect(findings).to_contain_text("欄に収まりません")
    page.get_by_role("button", name="生成する").click()
    expect(page.locator("#generate-form")).to_contain_text("警告を確かめてから生成してください")
    page.locator("#id_accept_warnings").check()
    page.get_by_role("button", name="生成する").click()

    expect(page.locator("#document-status")).to_have_text("生成しました。")
    expect(page.locator("#document-fields")).to_contain_text("第1版")
    with page.expect_download() as info:
        page.locator("#document-pdf").click()
    assert info.value.suggested_filename.startswith("1-17_")
    assert page.evaluate("document.documentElement.scrollWidth") <= width

    page.locator("#document-deliver").click()
    expect(page.locator("#delivery-generated")).to_contain_text("第1-17号")
    page.get_by_role("button", name="保存する").click()
    expect(page.get_by_text("交付記録を保存しました。")).to_be_visible()
    expect(page.locator("#delivery-fields")).to_contain_text("1-17 #")
    assert admin_requests == []
