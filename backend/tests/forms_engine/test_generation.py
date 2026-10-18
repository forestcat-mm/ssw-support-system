"""生成 → 生成履歴 → 認可つきのダウンロード（T29 の完了条件。ADR-0018）。

LibreOffice で実際に変換する（数秒かかるので数を絞る）。
"""

import datetime
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.db import connection
from django.test import Client

from apps.core.models import AuditAction, AuditLog
from apps.forms_engine.models import (
    FormMapping,
    GeneratedDocument,
    GenerationStatus,
    ImmutableRowError,
)
from apps.workers.models import ForeignWorker
from services import form_convert, form_generation
from tests.core.factories import make_user, verified_client
from tests.forms_engine import pdfcheck
from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db

ROOT = Path(settings.FORM_TEMPLATES_ROOT)
BASIS = datetime.date(2026, 10, 18)


@pytest.fixture(autouse=True)
def no_rules(loaded: None) -> None:
    """生成そのものを確かめるため、生成前の検査の規則を外す（検査は test_checks.py）。"""
    for mapping in FormMapping.objects.all():
        mapping.rules = []
        mapping.save()


@pytest.fixture
def worker(loaded: None) -> ForeignWorker:
    return make_worker(name_romaji="WANG FANG", birth_date=datetime.date(1995, 6, 20))


def generate(form_id: str, worker: ForeignWorker, data: Mapping[str, Any]) -> GeneratedDocument:
    document = form_generation.request(form_id, target=worker, data=data, basis_on=BASIS, by=None)
    document.refresh_from_db()
    return document


def test_excel_form_is_generated_and_matches_the_blank_form(worker: ForeignWorker) -> None:
    data = {
        "worker.name_romaji": "WANG FANG",
        "worker.birth_date": datetime.date(1995, 6, 20),
        "worker.nationality": "中国",
    }
    document = generate("1-17", worker, data)
    assert document.status == GenerationStatus.COMPLETED, document.failure_reason
    assert document.revision_unknown  # 改正日不明の版の印
    assert document.converter_version.startswith("LibreOffice")
    assert document.filled_sha256 and document.pdf_sha256

    pdf = Path(document.pdf_file.path)
    blank = pdfcheck.blank_pdf(ROOT / "1-17" / "2026-10-17" / "original.xlsx")
    assert document.page_count == len(pdfcheck.pdf_text(blank))
    assert pdfcheck.fixed_text_matches(pdf, blank, ["WANG FANG", 1995, 6, 20, "中国"])


def test_word_form_is_generated_and_matches_the_blank_form(worker: ForeignWorker) -> None:
    data = {
        "organization.name": "サンプル登録支援機関",
        "staff.name": "佐藤 花子",
        "session1.date": datetime.date(2026, 9, 17),
        "session1.start_hour": 14,
        "session1.start_minute": 0,
        "session1.end_hour": 16,
        "session1.end_minute": 30,
    }
    document = generate("5-9", worker, data)
    assert document.status == GenerationStatus.COMPLETED, document.failure_reason
    pdf = Path(document.pdf_file.path)
    blank = pdfcheck.blank_pdf(ROOT / "5-9" / "2026-10-17" / "original.docx")
    assert document.page_count == 1 == len(pdfcheck.pdf_text(blank))
    assert pdfcheck.fixed_text_matches(
        pdf, blank, ["サンプル登録支援機関", "佐藤 花子", 2026, 9, 17, 14, 0, 16, 30]
    )
    # PDF にパスワード・印刷禁止を付けない
    from pypdf import PdfReader

    assert not PdfReader(str(pdf)).is_encrypted


def test_history_is_new_each_time_encrypted_and_not_overwritten(worker: ForeignWorker) -> None:
    data = {"worker.name_romaji": "WANG FANG"}
    first = generate("1-17", worker, data)
    second = generate("1-17", worker, data)
    assert first.pk != second.pk  # 同じ内容の再生成は別の履歴
    assert json.loads(first.snapshot)["data"] == data
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT snapshot FROM forms_engine_generateddocument WHERE id = %s", [first.pk]
        )
        raw = cursor.fetchone()[0]
    assert "WANG" not in raw  # スナップショットは暗号化
    first.status = GenerationStatus.FAILED
    with pytest.raises(ImmutableRowError):
        first.save()
    with pytest.raises(ImmutableRowError):
        first.delete()
    created = AuditLog.objects.filter(action=AuditAction.GENERATE, object_id=str(first.pk)).get()
    assert created.context["form_id"] == "1-17"
    logs = AuditLog.objects.filter(
        content_type__app_label="forms_engine", object_id=str(first.pk)
    ).values("changes", "context")
    assert "WANG" not in json.dumps(list(logs), ensure_ascii=False)  # スナップショットは伏せる


def test_failure_keeps_no_output_and_a_reason_without_personal_data(
    worker: ForeignWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(path: Path) -> Path:
        raise form_convert.ConversionError("LibreOffice failed (exit code 1)")

    monkeypatch.setattr(form_convert, "convert", broken)
    document = generate("1-17", worker, {"worker.name_romaji": "WANG FANG"})
    assert document.status == GenerationStatus.FAILED
    assert document.failure_reason == "ConversionError: LibreOffice failed (exit code 1)"
    assert not document.pdf_file and not document.filled_file

    def unexpected(path: Path) -> Path:
        raise ValueError("WANG FANG is invalid")  # 個人情報を含む文言は残さない

    monkeypatch.setattr(form_convert, "convert", unexpected)
    document = generate("1-17", worker, {"worker.name_romaji": "WANG FANG"})
    assert document.failure_reason == "ValueError"


def test_pdf_download_needs_forms_generate_and_the_worker(worker: ForeignWorker) -> None:
    document = generate("5-9", worker, {"organization.name": "サンプル"})
    url = f"/forms/documents/{document.pk}/pdf/"
    staff = make_user("tanto2", roles=("support_staff",))
    client = verified_client(staff)
    response = client.get(url)
    assert response.status_code == 200
    assert response["Content-Disposition"].startswith("attachment")
    assert b"".join(response.streaming_content).startswith(b"%PDF")  # type: ignore[attr-defined]
    log = AuditLog.objects.filter(action=AuditAction.DOWNLOAD, object_id=str(document.pk)).get()
    assert log.context == {"form_id": "5-9", "file": "pdf"}

    no_forms = make_user("noforms", roles=("support_staff",))
    no_forms.user_roles.get().role.permissions.filter(action="forms.generate").delete()
    assert verified_client(no_forms).get(url).status_code == 403
    no_workers = make_user("noworkers", roles=("support_staff",))
    no_workers.user_roles.get().role.permissions.filter(action="workers.view").delete()
    assert verified_client(no_workers).get(url).status_code == 403
    assert Client().get(url).status_code in (302, 403)
    assert client.get("/forms/documents/999999/pdf/").status_code == 404


def test_failed_document_has_no_download(
    worker: ForeignWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(path: Path) -> Path:
        raise form_convert.ConversionError("LibreOffice timed out")

    monkeypatch.setattr(form_convert, "convert", broken)
    document = generate("5-9", worker, {})
    client = verified_client(make_user("tanto3", roles=("support_staff",)))
    assert client.get(f"/forms/documents/{document.pk}/pdf/").status_code == 403
