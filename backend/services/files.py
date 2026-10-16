"""アップロードファイルの検査と保存パス（モデルに依存しない）。

許可する拡張子・最大サイズ・先頭バイト（拡張子の偽装対策）を FilePolicy として持つ。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import PurePath
from typing import Any

from django.core.exceptions import ValidationError
from django.core.files import File
from django.template.defaultfilters import filesizeformat
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

MB = 1024 * 1024

# 拡張子ごとのファイル先頭バイト。
_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "pdf": (b"%PDF-",),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpg": (b"\xff\xd8\xff",),
    "jpeg": (b"\xff\xd8\xff",),
}


@dataclass(frozen=True)
class FilePolicy:
    extensions: frozenset[str]
    max_bytes: int

    def __post_init__(self) -> None:
        unknown = self.extensions - _SIGNATURES.keys()
        if unknown:
            raise ValueError(f"signature not defined for: {sorted(unknown)}")


LOGO_POLICY = FilePolicy(extensions=frozenset({"png", "jpg", "jpeg"}), max_bytes=2 * MB)


def extension_of(name: str) -> str:
    return PurePath(name).suffix.lower().lstrip(".")


def validate_file(file: File[Any], policy: FilePolicy) -> None:
    """拡張子・サイズ・先頭バイトを検査し、違反があれば ValidationError を送出する。"""
    ext = extension_of(file.name or "")
    if ext not in policy.extensions:
        raise ValidationError(
            _("File type “.%(ext)s” is not allowed. Allowed types: %(allowed)s."),
            code="invalid_extension",
            params={"ext": ext, "allowed": ", ".join(sorted(policy.extensions))},
        )
    if file.size is not None and file.size > policy.max_bytes:
        raise ValidationError(
            _("File is too large (%(size)s). The maximum size is %(max)s."),
            code="file_too_large",
            params={"size": filesizeformat(file.size), "max": filesizeformat(policy.max_bytes)},
        )
    if not _read_head(file).startswith(_SIGNATURES[ext]):
        raise ValidationError(
            _("File contents do not match the “.%(ext)s” extension."),
            code="signature_mismatch",
            params={"ext": ext},
        )


def validate_logo(file: File[Any]) -> None:
    validate_file(file, LOGO_POLICY)


def sha256_of(file: File[Any]) -> str:
    digest = hashlib.sha256()
    file.seek(0)
    for chunk in file.chunks():
        digest.update(chunk)
    file.seek(0)
    return digest.hexdigest()


def upload_path(prefix: str, filename: str, today: date | None = None) -> str:
    """元のファイル名（個人名を含みうる）を保存パスに使わず、UUID にする。"""
    today = today or timezone.localdate()
    ext = extension_of(filename)
    suffix = f".{ext}" if ext else ""
    return f"{prefix}/{today:%Y/%m}/{uuid.uuid4().hex}{suffix}"


def attachment_upload_to(instance: object, filename: str) -> str:
    return upload_path("attachments", filename)


def logo_upload_to(instance: object, filename: str) -> str:
    return upload_path("organization/logo", filename)


def _read_head(file: File[Any], length: int = 16) -> bytes:
    file.seek(0)
    head = file.read(length)
    file.seek(0)
    return head if isinstance(head, bytes) else head.encode()
