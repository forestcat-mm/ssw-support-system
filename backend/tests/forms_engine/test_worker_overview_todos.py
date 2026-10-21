"""外国人の概要の「今やること」: 作りかけの一括申請と提出用の PDF の下書き（U3-D3 の補い）。"""

import pytest
from django.utils import timezone

from services import bulk_applications, submission_drafts, worker_detail, worker_overview
from tests.forms_engine.bulk_cases import complete_application

pytestmark = pytest.mark.django_db

ALL = worker_overview.Allowed(contracts=True, support=True, filings=True, forms=True)


def todos_of(worker: object) -> list[worker_overview.TodoItem]:
    today = timezone.localdate()
    info = worker_detail.basic_info(worker)  # type: ignore[arg-type]
    head = worker_overview.header(worker, info, today)  # type: ignore[arg-type]
    return worker_overview.overview(worker, head, info, today, ALL).todos  # type: ignore[arg-type]


def test_unfinished_bulk_application_and_draft_are_listed(loaded: None) -> None:
    application = complete_application("extension", people=1)
    entry = application.entries.get()
    bulk_applications.save_inputs(entry, {}, sensitive=None, by=None)  # 申請ごとの入力を空に
    worker = entry.worker
    submission_drafts.save(worker, bulk_applications.set_id_of(application), {}, by=None)
    items = todos_of(worker)
    bulk = next(item for item in items if item.url.endswith(f"/forms/bulk/{application.pk}/"))
    assert bulk.state.startswith("不足") and bulk.state != "不足 0"
    draft = next(item for item in items if "application-set" in item.url or "set_id=" in item.url)
    assert draft.state.startswith("不足") and draft.tone == "caution"


def test_bulk_application_with_files_is_not_listed(loaded: None) -> None:
    application = complete_application("extension", people=1)
    bulk_applications.generate(application, by=None)
    worker = application.entries.get().worker
    assert not [item for item in todos_of(worker) if "/forms/bulk/" in item.url]
