from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile

from services.files import FilePolicy, upload_path, validate_file, validate_logo
from tests.conftest import PDF_BYTES, PNG_BYTES

POLICY = FilePolicy(extensions=frozenset({"pdf", "png"}), max_bytes=100)


def test_valid_file_passes() -> None:
    validate_file(ContentFile(PDF_BYTES, name="a.PDF"), POLICY)


@pytest.mark.parametrize(
    ("name", "content", "code"),
    [
        ("a.jpg", b"\xff\xd8\xff" + b"0" * 10, "invalid_extension"),
        ("noext", PDF_BYTES, "invalid_extension"),
        ("a.pdf", PDF_BYTES + b"0" * 100, "file_too_large"),
        ("a.png", PDF_BYTES, "signature_mismatch"),
    ],
)
def test_invalid_files_are_rejected(name: str, content: bytes, code: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        validate_file(ContentFile(content, name=name), POLICY)
    assert excinfo.value.code == code


def test_policy_requires_known_signature() -> None:
    with pytest.raises(ValueError):
        FilePolicy(extensions=frozenset({"docx"}), max_bytes=1)


def test_upload_path_does_not_use_original_name() -> None:
    path = upload_path("attachments", "在留カード_王.PDF", today=date(2026, 10, 16))
    assert path.startswith("attachments/2026/10/")
    assert path.endswith(".pdf")
    assert "王" not in path


def test_logo_allows_png_only_images() -> None:
    validate_logo(ContentFile(PNG_BYTES, name="logo.png"))
    with pytest.raises(ValidationError):
        validate_logo(ContentFile(PDF_BYTES, name="logo.pdf"))
