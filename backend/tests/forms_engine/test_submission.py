"""不足の書類の一括生成と提出用の統合 PDF（T42、ADR-0033）。

- 一覧の行ごとに生成前の検査の結果（生成不可・警告・生成できる）を出す。
- 一括生成は、生成不可を飛ばして知らせ、警告は確かめたときだけ生成する。同じ様式の行は 1 回。
- 統合 PDF は、提出書類一覧表を先頭に番号順。署名済みならスキャン、要配慮は担当者と管理者だけ。
- 容量の上限を超えそうなら警告。作ったことは生成履歴と監査ログに残す。
"""

import datetime
import io
import struct
import zlib
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from pypdf import PdfReader, PdfWriter
from pytest_django.fixtures import Settings as SettingsWrapper

from apps.core.models import AttachmentKind, AuditAction, AuditLog
from apps.forms_engine.models import (
    GeneratedDocument,
    GenerationStatus,
    ImmutableRowError,
    SubmissionBundle,
)
from apps.seed.context import DUMMY_PDF
from services import (
    application_bulk,
    application_sets,
    form_data_application,
    form_generation,
    form_signatures,
    form_templates,
    pdf_pages,
    submission_bundles,
    submission_drafts,
)
from services.attachments import create_attachment
from tests.core.factories import make_staff, make_user, verified_client
from tests.forms_engine.test_form_application import make_case

pytestmark = pytest.mark.django_db

BASIS = datetime.date(2026, 10, 18)


def one_page(text: str = "") -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    if text:
        writer.add_metadata({"/Subject": text})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def png(width: int = 30, height: int = 20, color_type: int = 2) -> bytes:
    def chunk(name: bytes, body: bytes) -> bytes:
        crc = struct.pack(">I", zlib.crc32(name + body))
        return struct.pack(">I", len(body)) + name + body + crc

    channels = {2: 3, 6: 4}[color_type]
    rows = b"".join(b"\x00" + bytes([10, 20, 30, 40][:channels] * width) for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        pdf_pages.PNG_SIGNATURE
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def jpeg(width: int = 40, height: int = 80) -> bytes:
    """SOF0 だけを持つ JPEG（中身は読まない。大きさと色の数だけを見る）。"""
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, height, width, 3) + b"\x00" * 9
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


# --- 画像と PDF の読み込み ------------------------------------------------------------------


def image_of(page: Any) -> Any:
    return page["/Resources"]["/XObject"]["/Im0"].get_object()


def test_images_become_one_a4_page_without_recompression() -> None:
    page = PdfReader(io.BytesIO(pdf_pages.as_pdf(png()))).pages[0]
    assert [round(float(v)) for v in page.mediabox[2:]] == [842, 595]  # 横長 → A4 横
    assert image_of(page).get_data() == b"\x0a\x14\x1e" * 30 * 20  # 予測子を戻すと元の画素
    data = jpeg()
    page = PdfReader(io.BytesIO(pdf_pages.as_pdf(data))).pages[0]
    assert [round(float(v)) for v in page.mediabox[2:]] == [595, 842]  # 縦長 → A4 縦
    assert image_of(page)._data == data  # そのまま


def cmyk_jpeg() -> bytes:
    from PIL import Image as PillowImage

    out = io.BytesIO()
    PillowImage.new("CMYK", (40, 30), (0, 128, 255, 0)).save(out, format="JPEG")
    return out.getvalue()


def test_transparent_png_and_cmyk_jpeg_become_pages_without_losing_pixels() -> None:
    """透過の PNG・CMYK の JPEG も白の地の RGB にして綴じる（画素の数は同じ。T42-D14）。"""
    cases = ((png(width=30, height=20, color_type=6), (30, 20)), (cmyk_jpeg(), (40, 30)))
    for data, size in cases:
        page = PdfReader(io.BytesIO(pdf_pages.as_pdf(data))).pages[0]
        image = image_of(page)
        assert (image["/Width"], image["/Height"]) == size
        assert image["/ColorSpace"] == "/DeviceRGB"


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"%PDF-1.4\n%broken\n", "broken"),
        (b"GIF89a", "unsupported"),
    ],
)
def test_unreadable_files(data: bytes, code: str) -> None:
    with pytest.raises(pdf_pages.UnreadableFile) as raised:
        pdf_pages.as_pdf(data)
    assert raised.value.code == code


def test_password_protected_pdf_is_unreadable() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("secret")
    out = io.BytesIO()
    writer.write(out)
    with pytest.raises(pdf_pages.UnreadableFile, match="encrypted"):
        pdf_pages.as_pdf(out.getvalue())


def test_dummy_pdf_of_the_seed_can_be_bound() -> None:
    assert len(pdf_pages.pdf_reader(DUMMY_PDF).pages) == 1


# --- 一括生成 -------------------------------------------------------------------------------


@pytest.fixture
def loaded(db: None) -> None:
    form_templates.load_all()


def candidates_by_id(overview: application_sets.Overview) -> dict[str, application_bulk.Candidate]:
    return {c.row_id: c for c in application_bulk.candidates(overview)}


def test_candidates_show_blocking_warnings_and_the_same_form(loaded: None) -> None:
    contract = make_case()
    overview = application_sets.overview(contract.worker, "chg", BASIS)
    found = candidates_by_id(overview)
    assert found["chg_t1-18-1"].problem == application_bulk.Problem.NO_PLAN  # 支援計画が無い
    assert found["chg_t1-18-1"].blocked
    assert found["chg_t1-6-2"].same_as == "chg_t1-6-1"  # 第1-6号の別紙は同じ生成物
    assert "chg_ind-2-1" in found and not found["chg_ind-2-1"].blocked  # 第3-1号
    checks = application_bulk.row_checks(overview)
    assert checks["chg_t1-18-1"].blocked
    assert not checks["chg_ind-2-1"].blocked
    assert "chg_t1-8-1" not in found  # 添付の行は生成しない
    assert "chg_t1-8-1" in [s.row["id"] for s in application_bulk.attachment_gaps(overview)]


def test_bulk_generation_skips_blocked_rows(loaded: None, settings: SettingsWrapper) -> None:
    settings.JOBS_INLINE = False  # 変換はしない（依頼の行だけを見る）
    contract = make_case()
    overview = application_sets.overview(contract.worker, "chg", BASIS)
    found = application_bulk.candidates(overview)
    every = [c.row_id for c in found]
    warned = [c for c in found if c.warnings and not c.blocked and not c.same_as]
    if warned:
        with pytest.raises(form_generation.WarningsNotAccepted):
            application_bulk.generate(found, every, basis_on=BASIS, by=None, accept_warnings=False)
        assert not GeneratedDocument.objects.exists()
    outcome = application_bulk.generate(found, every, basis_on=BASIS, by=None, accept_warnings=True)
    forms = sorted(c.form_id for c, _ in outcome.requested)
    assert forms == sorted({c.form_id for c in found if not c.blocked})
    skipped = sorted(c.form_id for c in outcome.skipped)
    assert skipped == sorted(c.form_id for c in found if c.blocked) and "1-17" in skipped
    assert GeneratedDocument.objects.filter(template__form_id="1-6").count() == 1
    assert all(
        d.status == GenerationStatus.REQUESTED and d.basis_on == BASIS
        for d in GeneratedDocument.objects.all()
    )


def test_bulk_screen(loaded: None, settings: SettingsWrapper) -> None:
    settings.JOBS_INLINE = False
    contract = make_case()
    worker = contract.worker
    client = verified_client(make_user("tanto42", roles=("support_staff",)))
    query = "?set_id=chg&basis_on=2026-10-18"
    listing = client.get(f"/workers/{worker.pk}/documents/application/{query}").content.decode()
    assert 'data-check="blocked"' in listing and 'id="application-bulk-link"' in listing
    url = f"/workers/{worker.pk}/documents/application/generate/{query}"
    page = client.get(url)
    content = page.content.decode()
    assert page.status_code == 200
    assert 'data-row="chg_t1-18-1" data-form="1-17" data-check="blocked"' in content
    assert 'id="bulk-gaps"' in content  # 健康診断の書類などの不足
    response = client.post(
        url,
        {
            page.context["once_field"]: page.context["once_token"],
            "set_id": "chg",
            "basis_on": "2026-10-18",
            "rows": ["chg_ind-2-1", "chg_t1-6-1"],
            "accept_warnings": "on",
            "action": "generate",
        },
    )
    assert response.status_code == 302
    assert sorted(GeneratedDocument.objects.values_list("template__form_id", flat=True)) == [
        "1-6",
        "3-1",
    ]


# --- 統合 PDF -------------------------------------------------------------------------------


def completed(form_id: str, target: Any, worker: Any, data: bytes) -> GeneratedDocument:
    """変換をしない、完了した生成物（PDF だけを持つ）。"""
    template = form_templates.select_template(form_id, BASIS).template
    document = GeneratedDocument.objects.create(
        template=template,
        target=target,
        worker=worker,
        basis_on=BASIS,
        snapshot="{}",
        status=GenerationStatus.REQUESTED,
    )
    document.pdf_file.save(f"{form_id}.pdf", ContentFile(data), save=False)
    document.status = GenerationStatus.COMPLETED
    document.generated_at = datetime.datetime(2026, 10, 18, 9, 0, tzinfo=datetime.UTC)
    document.page_count = 1
    document.save()
    return document


def bundle_case() -> tuple[object, GeneratedDocument]:
    contract = make_case()
    worker = contract.worker
    pledge = completed("3-1", contract, worker, one_page("3-1"))
    version = contract.versions.get()
    terms = completed("1-6", version, worker, one_page("1-6"))
    assert terms.template.requires_signed_copy
    form_signatures.add_signature(
        terms,
        [SimpleUploadedFile("signed.pdf", one_page("signed"), content_type="application/pdf")],
        signed_on=BASIS,
        by=None,
        today=BASIS,
    )
    contract_document = completed("1-5", version, worker, one_page("1-5"))
    health = create_attachment(
        target=worker,
        kind=AttachmentKind.HEALTH_CHECK_RECORD,
        file=SimpleUploadedFile("secret-health.pdf", DUMMY_PDF, content_type="application/pdf"),
        created_by=None,
    )
    health.examined_on = BASIS
    health.save()
    del pledge
    return contract, contract_document


def test_outline_order_parts_and_permissions(loaded: None) -> None:
    contract, contract_document = bundle_case()
    worker = contract.worker  # type: ignore[attr-defined]
    overview = application_sets.overview(worker, "chg", BASIS)
    admin = make_user("admin42", roles=("admin",))
    listing = SimpleUploadedFile("list.pdf", one_page("list"), content_type="application/pdf")
    submission_drafts.save(worker, "chg", {"chg_t1-2-1": [listing]}, by=None)
    outline = submission_bundles.outline(overview, admin)
    ids = [entry.row_id for entry in outline.entries]
    assert ids[0] == "chg_t1-2-1"  # 提出書類一覧表を先頭に
    entries = {entry.row_id: entry for entry in outline.entries}
    assert [p.source for p in entries["chg_t1-6-1"].parts] == ["signed"]  # 署名済みのスキャン
    assert entries["chg_t1-6-2"].same_as == "chg_t1-6-1"
    assert [p.source for p in entries["chg_t1-5-1"].parts] == ["generated"]
    assert submission_bundles.Warn.AWAITING_SIGNATURE in entries["chg_t1-5-1"].warnings
    health = entries["chg_t1-8-1"]
    assert [p.sensitive for p in health.bound] == [True]
    assert outline.contains_sensitive
    # 担当者でない支援担当者は、要配慮を綴じない
    other = make_user("tanto42", roles=("support_staff",))
    outline = submission_bundles.outline(overview, other)
    health = {e.row_id: e for e in outline.entries}["chg_t1-8-1"]
    assert not health.bound and submission_bundles.Warn.SENSITIVE_NOT_ALLOWED in health.warnings
    assert not outline.contains_sensitive
    # 担当者は綴じられる
    worker.assigned_staff = make_staff(user=other)
    worker.save()
    outline = submission_bundles.outline(overview, other)
    assert outline.contains_sensitive
    del contract_document


def test_build_keeps_the_history_and_the_audit_log(loaded: None, settings: SettingsWrapper) -> None:
    contract, _ = bundle_case()
    worker = contract.worker  # type: ignore[attr-defined]
    overview = application_sets.overview(worker, "chg", BASIS)
    admin = make_user("admin42", roles=("admin",))
    outline = submission_bundles.outline(overview, admin)
    assert outline.has_warnings
    with pytest.raises(ValidationError) as raised:
        submission_bundles.build(outline, by=admin, accept_warnings=False)
    assert raised.value.code == "warnings"
    settings.SUBMISSION_PDF_MAX_MB = 0  # 上限を超える
    outline = submission_bundles.outline(overview, admin)
    assert outline.over_limit
    bundle = submission_bundles.build(outline, by=admin, accept_warnings=True)
    reader = PdfReader(bundle.file.open("rb"))
    assert len(reader.pages) == bundle.page_count == sum(item["pages"] for item in bundle.items)
    assert [item["row"] for item in bundle.items][:1] == ["chg_t1-5-1"]
    assert {"row": "", "code": "over_limit"} in bundle.warnings
    assert bundle.contains_sensitive
    titles = [str(getattr(item, "title", "")) for item in reader.outline]
    assert titles[0].startswith("第１表 5")
    assert "secret-health" not in str(bundle.items)
    log = AuditLog.objects.filter(action=AuditAction.GENERATE, object_id=str(bundle.pk)).get()
    assert log.context["sensitive"] is True
    with pytest.raises(ImmutableRowError):
        bundle.save()
    with pytest.raises(ImmutableRowError):
        bundle.delete()


def test_download_needs_the_sensitive_permission(loaded: None) -> None:
    contract, _ = bundle_case()
    worker = contract.worker  # type: ignore[attr-defined]
    admin = make_user("admin42", roles=("admin",))
    overview = application_sets.overview(worker, "chg", BASIS)
    bundle = submission_bundles.build(
        submission_bundles.outline(overview, admin), by=admin, accept_warnings=True
    )
    url = f"/workers/{worker.pk}/documents/submission/{bundle.pk}/pdf/"
    other = verified_client(make_user("tanto42", roles=("support_staff",)))
    assert other.get(url).status_code == 403
    detail = other.get(f"/workers/{worker.pk}/documents/submission/{bundle.pk}/")
    assert detail.status_code == 200 and 'id="submission-download"' not in detail.content.decode()
    response = verified_client(admin).get(url)
    assert response.status_code == 200
    assert b"".join(response.streaming_content).startswith(b"%PDF")  # type: ignore[attr-defined]
    assert AuditLog.objects.filter(action=AuditAction.DOWNLOAD, object_id=str(bundle.pk)).exists()


def test_screen_with_an_upload(loaded: None) -> None:
    contract, _ = bundle_case()
    worker = contract.worker  # type: ignore[attr-defined]
    admin = make_user("admin42", roles=("admin",))
    client = verified_client(admin)
    url = f"/workers/{worker.pk}/documents/application/submission/?set_id=chg&basis_on=2026-10-18"
    page = client.get(url)
    content = page.content.decode()
    assert page.status_code == 200
    assert 'name="upload-chg_t1-2-1"' in content
    assert 'name="upload-chg_t1-1-1"' not in content or "返信用封筒" not in content
    assert "secret-health" not in content
    data = {
        page.context["once_field"]: page.context["once_token"],
        "set_id": "chg",
        "basis_on": "2026-10-18",
        "accept_warnings": "on",
        "upload-chg_t1-2-1": SimpleUploadedFile("list.png", png(), content_type="image/png"),
        "action": "save",
    }
    # 保存だけ: 下書きに残り、統合 PDF はまだ作らない
    response = client.post(url, data)
    assert response.status_code == 302 and not SubmissionBundle.objects.exists()
    draft = submission_drafts.draft_for(worker, "chg")
    saved = submission_drafts.files_by_row(draft)["chg_t1-2-1"]
    assert len(saved) == 1
    assert AuditLog.objects.filter(
        action=AuditAction.CREATE, content_type__model="submissiondraftfile"
    ).exists()
    # 作る: 保存したファイルを使う（アップロードし直さない）
    page = client.get(url)
    assert f'data-saved-file="{saved[0].pk}"' in page.content.decode()
    data[page.context["once_field"]] = page.context["once_token"]
    data["action"] = "make"
    del data["upload-chg_t1-2-1"]
    response = client.post(url, data)
    assert response.status_code == 302
    bundle = SubmissionBundle.objects.get()
    assert bundle.items[0]["row"] == "chg_t1-2-1" and bundle.items[0]["source"] == "upload"
    assert bundle.items[0]["object_id"] == saved[0].pk
    listing = client.get(
        f"/workers/{worker.pk}/documents/application/?set_id=chg&basis_on=2026-10-18"
    ).content.decode()
    assert f'data-bundle="{bundle.pk}"' in listing
    # 読めないファイルはアップロードの欄のエラー（保存しない）
    page = client.get(url)
    data[page.context["once_field"]] = page.context["once_token"]
    data["upload-chg_t1-2-1"] = SimpleUploadedFile("list.pdf", b"%PDF-1.4\n%broken\n")
    response = client.post(url, data)
    assert response.status_code == 200 and SubmissionBundle.objects.count() == 1
    assert len(submission_drafts.files_by_row(draft)["chg_t1-2-1"]) == 1
    # 取り除く（取消。行とファイルは残る）
    page = client.get(url)
    response = client.post(
        url,
        {
            page.context["once_field"]: page.context["once_token"],
            "set_id": "chg",
            "basis_on": "2026-10-18",
            f"remove-{saved[0].pk}": "on",
            "action": "save",
        },
    )
    assert response.status_code == 302
    assert "chg_t1-2-1" not in submission_drafts.files_by_row(draft)
    saved[0].refresh_from_db()
    assert saved[0].voided_at is not None


def test_form_data_application_is_unchanged(loaded: None) -> None:
    """一括生成のデータは 1 件ずつの画面と同じ（作成責任者の既定）。"""
    contract = make_case()
    overview = application_sets.overview(contract.worker, "chg", BASIS)
    found = candidates_by_id(overview)
    expected = form_data_application.build(
        "3-1",
        contract,
        BASIS,
        author=form_data_application.default_author("3-1", contract),
    )
    assert found["chg_ind-2-1"].data == expected
