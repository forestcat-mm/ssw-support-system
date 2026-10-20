"""通しのテスト: 一括申請の項目（T46a）。外国人詳細の「一括申請の項目」から、在日親族・同居者と
技能水準・日本語能力を足す。番号は先頭・末尾 2 文字だけ出る。管理画面には行かない。"""

from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Request, expect

from apps.core.models import User
from apps.workers.models import ForeignWorker, WorkerStatus
from e2e.conftest import SignIn
from tests.conftest import PDF_BYTES
from tests.workers.factories import make_worker


@pytest.fixture
def worker() -> ForeignWorker:
    return make_worker(status=WorkerStatus.RESIDING)


def watch_admin(page: Page) -> list[str]:
    seen: list[str] = []

    def watch(request: Request) -> None:
        if urlparse(request.url).path.startswith("/admin/"):
            seen.append(request.url)

    page.on("request", watch)
    return seen


@pytest.mark.parametrize("width", [1366, 390])
def test_add_a_relative_and_a_qualification(
    page: Page, sign_in: SignIn, support_staff: User, worker: ForeignWorker, width: int
) -> None:
    admin_requests = watch_admin(page)
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(support_staff)
    base = page.url.rstrip("/")
    page.goto(f"{base}/workers/{worker.pk}/")
    items = page.locator("#application-items")
    expect(items).to_contain_text("一括申請の項目")
    assert page.evaluate("document.documentElement.scrollWidth") <= width

    page.locator("#relative").get_by_role("link", name="追加").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("在日親族・同居者を追加")
    page.get_by_label("続柄").select_option(label="妻")
    page.get_by_label("氏名").fill("WANG LI")
    page.get_by_label("国籍・地域").select_option(label="中国")
    page.get_by_label("同居（予定）").check()
    page.get_by_label("在留カード番号・特別永住者証明書番号").fill("AB12345678CD")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.get_by_role("button", name="保存する").click()
    relatives = page.locator('[data-section="relative"] li')
    expect(relatives).to_have_count(1)
    expect(relatives.first).to_contain_text("WANG LI")
    expect(relatives.first).to_contain_text("AB********CD")

    page.locator("#qualification").get_by_role("link", name="追加").click()
    page.locator("#id_area").select_option(label="日本語能力")
    page.get_by_label("評価区分").select_option(label="特定技能評価試験")
    page.get_by_label("合格した試験").select_option(label="日本語能力試験")
    page.get_by_label("レベル").fill("N4")
    page.get_by_label("証明書（スキャン）").set_input_files(
        {"name": "合格証明書.pdf", "mimeType": "application/pdf", "buffer": PDF_BYTES}
    )
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.get_by_role("button", name="保存する").click()
    qualifications = page.locator('[data-section="qualification"] li')
    expect(qualifications.first).to_contain_text("日本語能力試験")
    expect(qualifications.first).to_contain_text("合格証明書.pdf")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    assert admin_requests == []
