"""PDF への変換（LibreOffice headless。docx と xlsx を受ける。T29、ADR-0018）。

CLAUDE.md の「Word→PDF は 1 関数に閉じる」を、Excel の様式（第1-17号・第5-5号・5-6号）にも
広げた convert(path) -> pdf。変換のたびに別のユーザープロファイルを使い（同時に動かしても
ぶつからない）、時間の上限を設ける。PDF にパスワード・印刷禁止は付けない。
"""

from __future__ import annotations

import functools
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings

SOFFICE = "soffice"


class ConversionError(Exception):
    """変換に失敗した（理由は個人情報を含まない文言にする）。"""


def convert(path: Path) -> Path:
    """path（.docx / .xlsx）を同じフォルダに PDF として出し、そのパスを返す。"""
    if path.suffix.lower() not in (".docx", ".xlsx"):
        raise ConversionError(f"unsupported file type: {path.suffix}")
    with tempfile.TemporaryDirectory(prefix="lo-profile-") as profile:
        command = [
            SOFFICE,
            "--headless",
            "--norestore",
            "--nolockcheck",
            f"-env:UserInstallation=file://{profile}",
            "--convert-to",
            "pdf",
            "--outdir",
            str(path.parent),
            str(path),
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                timeout=settings.FORM_CONVERT_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise ConversionError("LibreOffice timed out") from error
        except FileNotFoundError as error:
            raise ConversionError("LibreOffice is not installed") from error
    pdf = path.with_suffix(".pdf")
    if result.returncode != 0 or not pdf.is_file():
        raise ConversionError(f"LibreOffice failed (exit code {result.returncode})")
    return pdf


@functools.cache
def converter_version() -> str:
    """変換の環境（LibreOffice の版）。生成履歴に残す。"""
    if shutil.which(SOFFICE) is None:
        return ""
    result = subprocess.run([SOFFICE, "--version"], capture_output=True, text=True, check=False)
    return result.stdout.strip()[:100]
