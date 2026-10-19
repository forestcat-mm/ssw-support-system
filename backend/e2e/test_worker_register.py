"""通しのテスト 2: 外国人の登録 → 詳細 → 在留情報の追加 → 一覧の在留期限が変わる（T13d）。"""

import re
from datetime import timedelta

import pytest
from django.utils import timezone
from playwright.sync_api import Page, expect

from apps.core.models import User
from apps.workers.models import ForeignWorker
from e2e.conftest import SignIn
from tests.workers.factories import add_residence, make_worker


def fill_basic(page: Page, *, romaji: str, kanji: str, status: str) -> None:
    page.get_by_label("氏名（ローマ字）").fill(romaji)
    page.get_by_label("氏名（漢字）").fill(kanji)
    page.get_by_label("国籍・地域").select_option(label="中国")
    # 生年月日は年・月・日の選択（修正 1 回目 B16）
    page.locator("#id_birth_date_year").select_option("1999")
    page.locator("#id_birth_date_month").select_option("1")
    page.locator("#id_birth_date_day").select_option("2")
    page.get_by_label("性別").select_option(label="男")
    page.get_by_label("支援言語").select_option(label="中国語（簡体字）")
    page.get_by_label(re.compile(r"^状態")).select_option(label=status)


@pytest.mark.parametrize("width", [1366, 390])
def test_register_then_add_residence_and_see_expiry_in_list(
    page: Page, sign_in: SignIn, support_staff: User, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    sign_in(support_staff)
    base = page.url.rstrip("/")
    page.goto(f"{base}/workers/")
    page.get_by_role("link", name="登録する").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("外国人の登録")

    fill_basic(page, romaji="CHEN JIE", kanji="陳傑", status="入国前")
    page.get_by_role("button", name="登録する").click()

    expect(page.get_by_text("外国人を登録しました。")).to_be_visible()
    expect(page.get_by_role("heading", level=1)).to_contain_text("陳傑")
    expect(page.locator("#residence")).to_contain_text("未登録")
    assert page.evaluate("document.documentElement.scrollWidth") <= width

    # 在留情報を 1 行追加（番号は空白・小文字でも保存のときに整える）
    page.locator("#residence").get_by_role("link", name="追加").click()
    page.get_by_label("申請種別").select_option(label="在留資格変更")
    page.get_by_label("在留資格").select_option(label="特定技能1号")
    page.get_by_label("在留カード番号").fill("ab 1234 5678 cd")
    expires_on = timezone.localdate() + timedelta(days=10)
    page.get_by_label("許可日").fill((expires_on - timedelta(days=365)).isoformat())
    page.get_by_label("在留期限").fill(expires_on.isoformat())
    page.get_by_role("button", name="保存する").click()

    expect(page.locator('[data-decrypted="card_number"]')).to_have_text("AB12345678CD")

    page.goto(f"{base}/workers/")
    worker = ForeignWorker.objects.get(name_romaji="CHEN JIE")
    row = page.locator(f'#worker-list tr[data-worker="{worker.pk}"]')
    expect(row).to_contain_text(expires_on.isoformat())
    expect(row.locator('[data-level="within_14"]')).to_be_visible()


def test_number_of_another_worker_is_rejected_with_id_only(
    page: Page, sign_in: SignIn, support_staff: User
) -> None:
    other = make_worker(name_romaji="OTHER PERSON", name_kanji="別人")
    add_residence(other, card_number="AB12345678CD")
    me = make_worker(name_romaji="CHEN JIE", name_kanji="陳傑", status="residing")
    sign_in(support_staff)
    page.goto(page.url.rstrip("/") + f"/workers/{me.pk}/residence/new/")

    page.get_by_label("申請種別").select_option(label="在留期間更新")
    page.get_by_label("在留資格").select_option(label="特定技能1号")
    page.get_by_label("在留カード番号").fill("AB12345678CD")
    page.get_by_label("許可日").fill("2026-04-01")
    page.get_by_label("在留期限").fill("2027-04-01")
    page.get_by_role("button", name="保存する").click()

    error = page.locator("#id_card_number-error")
    expect(error).to_contain_text(f"ID: {other.pk}")
    expect(error).not_to_contain_text("別人")
    expect(page.get_by_label("在留カード番号")).to_have_attribute("aria-invalid", "true")
    assert not me.residence_records.exists()
