"""生成した PDF を、公式の様式を空のまま同じ方法で変換した PDF と比べる道具（テスト用。T29）。

段階3の完了条件 A の「様式との一致」①: 頁数と固定の文言が同じ。
固定の文言は、生成した PDF の本文から差し込んだ値を除き、空白を除いて比べる。
"""

from __future__ import annotations

import re
import shutil
import tempfile
from collections import Counter
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


def fixed_characters_match(
    generated: Path, blank: Path, written: Iterable[object], printed: Iterable[object]
) -> bool:
    """選択肢の文字を置き換える様式（第1-17号）の固定の文言の比べ方（T31）。

    生成した PDF の文字に、書き込み先の印刷済みの文字を足したものと、空の様式の PDF の文字に
    書いた値を足したものが、文字ごとの数で同じ（空白は除く）。並びの順は見ない（PDF の文字の
    取り出し順が、値の長さで変わるため）。頁数は別に比べる。
    """

    def count(texts: Iterable[object]) -> Counter[str]:
        return Counter(_squash("".join(str(text) for text in texts if text is not None)))

    left = count(pdf_text(generated)) + count(printed)
    right = count(pdf_text(blank)) + count(written)
    return left == right
