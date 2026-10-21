"""申請用の写真の知らせ（U4-D3・D4）。

申請の書類の一覧と一括申請の行に、写真が無い・古いことを出す。
"""

from datetime import date

import pytest
from django.test import Client

from services import bulk_applications, worker_photos
from tests.forms_engine.bulk_cases import complete_application
from tests.workers.test_worker_photos import jpeg

pytestmark = pytest.mark.django_db


def test_application_set_shows_a_missing_or_old_photo(loaded: None, admin_client: Client) -> None:
    application = complete_application("extension", people=1)
    worker = application.entries.get().worker
    set_id = bulk_applications.set_id_of(application)
    url = f"/workers/{worker.pk}/documents/application/?set_id={set_id}&basis_on=2026-10-21"
    html = admin_client.get(url).content.decode()
    assert 'id="photo-notice"' in html and "申請用の写真が登録されていません。" in html
    worker_photos.register(worker, jpeg(), taken_on=date(2026, 3, 1), box=None, by=None)
    html = admin_client.get(url).content.decode()
    assert "写真が古い（2026-03-01 撮影）" in html
    html = admin_client.get(f"/forms/bulk/{application.pk}/").content.decode()
    assert "data-photo-notice" in html


def test_no_notice_for_a_recent_photo(loaded: None, admin_client: Client) -> None:
    application = complete_application("extension", people=1)
    worker = application.entries.get().worker
    worker_photos.register(worker, jpeg(), taken_on=application.basis_on, box=None, by=None)
    html = admin_client.get(f"/forms/bulk/{application.pk}/").content.decode()
    assert "data-photo-notice" not in html
