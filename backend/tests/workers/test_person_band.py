"""外国人に関わる入力の画面の上に留める細い帯（U6。仕様 5.3・見本 07）。"""

import pytest
from django.test import Client

from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db


def test_worker_forms_show_the_band_instead_of_the_search(admin_client: Client) -> None:
    worker = make_worker(name_kanji="王芳")
    html = admin_client.get(f"/workers/{worker.pk}/edit/").content.decode()
    assert 'id="person-band"' in html
    assert "data-form-toc" in html
    assert 'id="global-search"' not in html  # 帯を出す画面は全体の検索の帯を出さない
    assert f'href="/workers/{worker.pk}/"' in html  # 戻る
    assert 'id="form-bar-text"' in html


def test_band_marks_the_tab_of_the_screen(admin_client: Client) -> None:
    worker = make_worker()
    html = admin_client.get(f"/workers/{worker.pk}/contracts/new/").content.decode()
    band = html[html.index('id="person-band"') :]
    assert f'href="/workers/{worker.pk}/contracts/" aria-current="page"' in band


def test_detail_and_other_screens_keep_their_own_head(admin_client: Client) -> None:
    worker = make_worker()
    detail = admin_client.get(f"/workers/{worker.pk}/").content.decode()
    assert 'id="person-band"' not in detail and 'id="person-head"' in detail
    hosts = admin_client.get("/hosts/new/").content.decode()
    assert 'id="person-band"' not in hosts and 'id="global-search"' in hosts
