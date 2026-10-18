"""生成した PDF を、公式の様式を空のまま同じ方法で変換した PDF と比べる道具（テスト用。T29）。

段階3の完了条件 A の「様式との一致」①: 頁数と固定の文言が同じ。
固定の文言は、生成した PDF の本文から差し込んだ値を除き、空白を除いて比べる。
"""

from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Iterable
from pathlib import Path

from pypdf import PdfReader

from services import form_convert


def pdf_text(path: Path) -> list[str]:
    return [page.extract_text() or "" for page in PdfReader(str(path)).pages]


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def blank_pdf(original: Path) -> Path:
    """公式の様式を空のまま、生成と同じ方法（convert）で PDF にする。"""
    workdir = Path(tempfile.mkdtemp(prefix="blank-"))
    copy = workdir / f"blank{original.suffix}"
    shutil.copyfile(original, copy)
    return form_convert.convert(copy)


def fixed_text_matches(generated: Path, blank: Path, inserted: Iterable[object]) -> bool:
    generated_text = _squash("".join(pdf_text(generated)))
    for value in sorted({str(v) for v in inserted if str(v)}, key=len, reverse=True):
        generated_text = generated_text.replace(_squash(value), "", 1)
    return generated_text == _squash("".join(pdf_text(blank)))
