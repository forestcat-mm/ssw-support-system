"""通しのテスト: 申請用の写真（U4。ADR-0047）。

登録 → 上の帯に出る → 差し替え → 履歴 → JPEG のダウンロード。
"""

import io
from pathlib import Path
from urllib.parse import urlparse

import pytest
from django.utils import timezone
from PIL import Image
from playwright.sync_api import Page, Request, expect

from apps.core.models import User
from apps.workers.models import ForeignWorker, WorkerPhoto
from e2e.conftest import SignIn
from tests.workers.factories import make_worker


def write_jpeg(path: Path, color: tuple[int, int, int]) -> Path:
    buffer = io.BytesIO()
    Image.new("RGB", (600, 800), color).save(buffer, format="JPEG")
    path.write_bytes(buffer.getvalue())
    return path


@pytest.fixture
def worker() -> ForeignWorker:
    return make_worker()


@pytest.mark.parametrize("width", [1366, 390])
def test_register_replace_and_download_the_photo(
    page: Page,
    sign_in: SignIn,
    administrator: User,
    worker: ForeignWorker,
    tmp_path: Path,
    width: int,
) -> None:
    admin_requests: list[str] = []

    def watch(request: Request) -> None:
        if urlparse(request.url).path.startswith("/admin/"):
            admin_requests.append(request.url)

    page.on("request", watch)
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(administrator)
    base = page.url.rstrip("/")
    page.goto(f"{base}/workers/{worker.pk}/")
    expect(page.locator("#person-photo .person-photo__initial")).to_have_text("王")

    # 写真がまだ無いので「写真を登録する」。画像を選ぶと 3:4 の枠が出る（矢印のキーで動かせる）
    page.locator("#person-photo-line").get_by_role("link", name="写真を登録する").click()
    page.get_by_label("写真のファイル").set_input_files(
        write_jpeg(tmp_path / "a.jpg", (200, 120, 80))
    )
    frame = page.get_by_role("slider", name="切り抜きの枠（3:4）")
    expect(frame).to_be_visible()
    frame.focus()
    page.keyboard.press("Shift+ArrowLeft")  # 少し小さくする
    page.get_by_label("撮影日").fill(timezone.localdate().strftime("%Y-%m-%d"))
    page.get_by_role("button", name="登録する").click()
    expect(page.get_by_text("申請用の写真を登録しました。")).to_be_visible()
    first = WorkerPhoto.objects.get()
    assert (
        first.crop_height * 3 == first.crop_width * 4
        or abs(first.crop_height * 3 - first.crop_width * 4) <= 4
    )
    expect(page.locator("#person-photo img")).to_be_visible()

    # 差し替え: 前の写真は履歴に残る
    page.locator("#person-photo-line").get_by_role("link", name="写真を変える").click()
    page.get_by_label("写真のファイル").set_input_files(
        write_jpeg(tmp_path / "b.jpg", (80, 120, 200))
    )
    page.get_by_label("撮影日").fill(timezone.localdate().strftime("%Y-%m-%d"))
    page.get_by_role("button", name="登録する").click()
    expect(page.get_by_text("申請用の写真を登録しました。")).to_be_visible()
    page.get_by_role("navigation", name="区分").get_by_role("link", name="履歴").click()
    rows = page.locator("#photo-history [data-photo]")
    expect(rows).to_have_count(2)
    expect(rows.first).to_contain_text("使用中")

    with page.expect_download() as info:
        rows.first.get_by_role("link", name="ダウンロード（JPEG）").click()
    assert info.value.suggested_filename.startswith("WANG_FANG_")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    assert admin_requests == []
