"""帳票タブと署名の流れ（T33、ADR-0022）。

- 署名の状態は、取り消していない添付の紐づけから決める（生成物は変えない）。
- 確認書は記録の確認書の添付（T21 の「署名済み」がそのまま使える）、記録の無い様式は生成物の添付。
- 回収のタスク: 「署名済み添付が必須」の様式だけ、生成から設定の日数で起票。署名で自動完了、
  新しい生成物で「不要」、添付の取消で未着手に戻る、元の記録の取消は共通の規則。
"""

import datetime
import json
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.core.models import AttachmentKind, AuditAction, AuditLog, Organization
from apps.filings.models import Filing, FilingKind, FilingStatus, Trigger
from apps.forms_engine.models import (
    FormTemplate,
    GeneratedDocument,
    GeneratedDocumentSignature,
    GenerationStatus,
    ImmutableRowError,
)
from apps.support.models import Briefing
from services import attachments, briefings, filings, form_signatures, workers
from tests.conftest import PDF_BYTES
from tests.contracts.factories import make_contract, site_with_categories
from tests.core.factories import make_organization, make_staff, make_user, verified_client
from tests.support.test_briefings import create, orientation
from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db

GENERATED_ON = datetime.date(2026, 10, 18)


def pdf(name: str = "signed.pdf") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, PDF_BYTES, content_type="application/pdf")


@pytest.fixture
def worker(loaded: None) -> Any:
    make_organization()
    worker = make_worker(assigned_staff=make_staff(name="佐藤 花子", position="支援担当"))
    make_contract(worker, site=site_with_categories())
    return worker


def completed(
    worker: Any, form_id: str, target: Any = None, *, on: datetime.date = GENERATED_ON
) -> GeneratedDocument:
    """変換をせずに、完了した生成物の行を作る（署名と回収の試験用）。"""
    at = timezone.make_aware(datetime.datetime.combine(on, datetime.time(10)))
    return GeneratedDocument.objects.create(
        template=FormTemplate.objects.get(form_id=form_id),
        target=target or worker,
        worker=worker,
        basis_on=on,
        snapshot="{}",
        status=GenerationStatus.COMPLETED,
        generated_at=at,
    )


def collection_tasks(document: GeneratedDocument) -> list[Filing]:
    return list(
        Filing.objects.filter(
            kind=FilingKind.SIGNED_COPY_COLLECTION,
            content_type=ContentType.objects.get_for_model(GeneratedDocument),
            object_id=document.pk,
        )
    )


def test_manifests_require_signed_copies() -> None:
    """回収の対象は manifest の「署名済み添付が必須」が true のものだけ（3 様式とも true）。"""
    root = Path(settings.FORM_TEMPLATES_ROOT)
    values = {
        form_id: json.loads((root / form_id / "2026-10-17" / "manifest.json").read_text("utf-8"))[
            "requires_signed_copy"
        ]
        for form_id in ("1-17", "5-8", "5-9")
    }
    assert values == {"1-17": True, "5-8": True, "5-9": True}


# --- 署名済みの添付 ---------------------------------------------------------------------


def test_signature_on_a_form_without_a_record_attaches_to_the_document(worker: Any) -> None:
    document = completed(worker, "1-17")
    assert form_signatures.state(document) == form_signatures.SignatureState.AWAITING
    [row] = form_signatures.add_signature(
        document, [pdf()], signed_on=GENERATED_ON, by=None, today=GENERATED_ON
    )
    assert row.attachment.kind == AttachmentKind.SIGNED_DOCUMENT
    assert row.attachment.target == document
    assert form_signatures.state(document) == form_signatures.SignatureState.SIGNED
    assert form_signatures.signed_on(document) == GENERATED_ON
    with pytest.raises(ImmutableRowError):
        row.save()
    with pytest.raises(ImmutableRowError):
        row.delete()
    document.refresh_from_db()  # 生成物は変えていない
    assert document.status == GenerationStatus.COMPLETED


@pytest.mark.parametrize(
    ("files", "signed_on", "key"),
    [
        ([], GENERATED_ON, "files"),
        (["x"], None, "signed_on"),
        (["x"], GENERATED_ON + datetime.timedelta(days=1), "signed_on"),  # 未来
        (["x"], GENERATED_ON - datetime.timedelta(days=1), "signed_on"),  # 生成の前
    ],
)
def test_signature_input_is_checked(
    worker: Any, files: list[str], signed_on: datetime.date | None, key: str
) -> None:
    document = completed(worker, "1-17")
    with pytest.raises(ValidationError) as error:
        form_signatures.add_signature(
            document, [pdf() for _ in files], signed_on=signed_on, by=None, today=GENERATED_ON
        )
    assert key in error.value.error_dict


def test_signature_on_a_confirmation_attaches_to_the_record(worker: Any) -> None:
    """確認書は記録の確認書の添付にし、T21 の「署名済み」の判定がそのまま使える。"""
    briefing = create(worker, orientation(worker))
    document = completed(worker, "5-8", briefing, on=datetime.date(2026, 10, 17))
    assert "unsigned" in [w.code for w in briefings.warnings(briefing)]
    [row] = form_signatures.add_signature(
        document,
        [pdf()],
        signed_on=datetime.date(2026, 10, 17),
        by=None,
        today=datetime.date(2026, 10, 17),
    )
    assert row.attachment.target == briefing.record
    assert row.attachment.kind == AttachmentKind.ORIENTATION_CONFIRMATION
    briefing.refresh_from_db()
    assert briefings.is_signed(briefing)
    assert "unsigned" not in [w.code for w in briefings.warnings(briefing)]


# --- 回収のタスク -----------------------------------------------------------------------


def test_collection_task_is_raised_after_the_set_days_and_completed_by_the_signature(
    worker: Any,
) -> None:
    document = completed(worker, "1-17")
    assert form_signatures.raise_collection_tasks(GENERATED_ON + datetime.timedelta(days=13)) == []
    [task] = form_signatures.raise_collection_tasks(GENERATED_ON + datetime.timedelta(days=14))
    assert task.due_on == GENERATED_ON + datetime.timedelta(days=14) == task.opens_on
    assert task.worker == worker and task.target == document
    assert form_signatures.raise_collection_tasks(GENERATED_ON + datetime.timedelta(days=15)) == []
    # 手では取消・完了にできない（「不要」はできる）
    allowed = filings.allowed_transitions(task)
    assert FilingStatus.CANCELLED not in allowed and FilingStatus.COMPLETED not in allowed
    assert FilingStatus.NOT_REQUIRED in allowed

    form_signatures.add_signature(
        document, [pdf()], signed_on=GENERATED_ON, by=None, today=GENERATED_ON
    )
    task.refresh_from_db()
    assert task.status == FilingStatus.COMPLETED
    log = AuditLog.objects.filter(action=AuditAction.FILING_TRANSITION, object_id=str(task.pk))
    assert log.last().context["automatic"] is True  # type: ignore[union-attr]

    # 署名の添付を取り消すと、署名待ちに戻り、タスクも未着手に戻る
    attachment = form_signatures.live_signatures(document).get().attachment
    attachments.void_attachment(attachment, by=None, reason="別人のスキャンだった")
    task.refresh_from_db()
    assert task.status == FilingStatus.NOT_STARTED
    assert form_signatures.state(document) == form_signatures.SignatureState.AWAITING


def test_days_come_from_the_organization_setting(worker: Any) -> None:
    Organization.objects.update(signed_copy_due_days=3)
    completed(worker, "1-17")
    assert (
        len(form_signatures.raise_collection_tasks(GENERATED_ON + datetime.timedelta(days=3))) == 1
    )


def test_a_newer_document_closes_the_old_task(worker: Any) -> None:
    old = completed(worker, "1-17")
    [task] = form_signatures.raise_collection_tasks(GENERATED_ON + datetime.timedelta(days=14))
    newer = completed(worker, "1-17", on=GENERATED_ON + datetime.timedelta(days=15))
    form_signatures.raise_collection_tasks(GENERATED_ON + datetime.timedelta(days=16))
    task.refresh_from_db()
    assert task.status == FilingStatus.NOT_REQUIRED
    assert collection_tasks(newer) == []  # 新しい生成物は、まだ日数がたっていない
    assert old.pk != newer.pk


def test_no_task_for_forms_without_signed_copies_or_deactivated_workers(worker: Any) -> None:
    FormTemplate.objects.filter(form_id="1-17").update(requires_signed_copy=False)
    completed(worker, "1-17")
    assert form_signatures.raise_collection_tasks(GENERATED_ON + datetime.timedelta(days=30)) == []
    FormTemplate.objects.filter(form_id="1-17").update(requires_signed_copy=True)
    workers.deactivate(worker, by=None, reason="帰国")
    assert form_signatures.raise_collection_tasks(GENERATED_ON + datetime.timedelta(days=30)) == []


def test_daily_job_raises_the_task(worker: Any) -> None:
    completed(worker, "1-17")
    result = filings.create_scheduled(GENERATED_ON + datetime.timedelta(days=14))
    assert [f.kind for f in result.created if f.kind == FilingKind.SIGNED_COPY_COLLECTION] == [
        FilingKind.SIGNED_COPY_COLLECTION
    ]


def test_voiding_the_record_reverts_the_task_by_the_common_rule(worker: Any) -> None:
    briefing = create(worker, orientation(worker))
    document = completed(worker, "5-8", briefing, on=datetime.date(2026, 10, 17))
    [task] = form_signatures.raise_collection_tasks(datetime.date(2026, 10, 31))
    briefings.void_briefing(briefing, by=None, reason="日付の誤り")
    task.refresh_from_db()
    assert task.status == FilingStatus.CANCELLED  # 未着手 → 取消
    assert not form_signatures.can_add_signature(document)


# --- 画面 ------------------------------------------------------------------------------


def test_documents_tab_signature_and_download(worker: Any) -> None:
    document = completed(worker, "1-17")
    briefing = create(worker, orientation(worker))
    briefings.add_confirmation(
        briefing,
        [pdf()],
        signed_on=datetime.date(2026, 10, 17),
        by=None,
        today=datetime.date(2026, 10, 17),
    )
    staff = make_user("tanto6", roles=("support_staff",))
    client = verified_client(staff)
    tab = client.get(f"/workers/{worker.pk}/documents/")
    assert tab.status_code == 200
    content = tab.content.decode()
    assert f'data-document="{document.pk}" data-state="awaiting"' in content
    assert 'id="legacy-signed"' in content  # 生成履歴の無い T21 の署名済みの確認書
    log = AuditLog.objects.filter(action=AuditAction.VIEW, object_id=str(worker.pk)).last()
    assert log is not None and log.context["shown"]["generated_documents"] == [document.pk]

    url = f"/workers/{worker.pk}/documents/{document.pk}/signature/"
    page = client.get(url)
    response = client.post(
        url,
        {
            page.context["once_field"]: page.context["once_token"],
            "signed_on": timezone.localdate().isoformat(),
            "scans": pdf(),
        },
    )
    assert response.status_code == 302
    row = GeneratedDocumentSignature.objects.get(document=document)
    assert client.get(f"/attachments/{row.attachment.pk}/download/").status_code == 200
    no_forms = make_user("noforms3", roles=("support_staff",))
    no_forms.user_roles.get().role.permissions.filter(action="forms.generate").delete()
    other = verified_client(no_forms)
    assert other.get(f"/attachments/{row.attachment.pk}/download/").status_code == 403
    assert other.get(f"/workers/{worker.pk}/documents/").status_code == 403


def test_stale_requests_are_shown(worker: Any) -> None:
    document = GeneratedDocument.objects.create(
        template=FormTemplate.objects.get(form_id="1-17"),
        target=worker,
        worker=worker,
        basis_on=GENERATED_ON,
        snapshot="{}",
    )
    GeneratedDocument.objects.filter(pk=document.pk).update(
        created_at=timezone.now() - datetime.timedelta(minutes=11)
    )
    client = verified_client(make_user("tanto5", roles=("support_staff",)))
    content = client.get(f"/forms/documents/{document.pk}/").content.decode()
    assert "qcluster" in content and "10 分" in content


def test_filing_links_generated_documents_of_the_same_worker(worker: Any) -> None:
    document = completed(worker, "1-17")
    filing, _ = filings.create(
        FilingKind.RESIDENCE_RENEWAL,
        target=worker,
        basis_on=datetime.date(2027, 1, 1),
        trigger=Trigger.MANUAL,
    )
    filings.link_document(filing, document, by=None)
    assert list(filing.generated_documents.all()) == [document]
    other = completed(make_worker(name_romaji="LI MING"), "1-17")
    with pytest.raises(ValidationError):
        filings.link_document(filing, other, by=None)
    client = verified_client(make_user("kanri2", roles=("admin",)))
    assert (
        f"/forms/documents/{document.pk}/" in client.get(f"/filings/{filing.pk}/").content.decode()
    )
    filings.unlink_document(filing, document, by=None)
    assert not filing.generated_documents.exists()


def test_run_twice_does_nothing_after_completion(worker: Any) -> None:
    from services import form_generation

    document = completed(worker, "1-17")
    form_generation.run(document.pk)  # 完了した生成物は変えない
    document.refresh_from_db()
    assert document.status == GenerationStatus.COMPLETED


def test_briefing_has_no_extra_columns() -> None:
    """Briefing.signed_on は変えない（帳票タブは紐づけの行の署名日を出す。ADR-0022）。"""
    assert [f.name for f in Briefing._meta.get_fields() if f.name == "signed_on"] == ["signed_on"]
