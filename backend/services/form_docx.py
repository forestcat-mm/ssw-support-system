"""Word の様式に差し込む（docxtpl。T29、ADR-0018）。

template.docx は、入管の original の写しに差込タグ（{{ 名前 }}）を置くことだけをしたもの。
タグを外した本文が original の本文と同じことをテストで確かめる（tests/forms_engine）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from docxtpl import DocxTemplate  # type: ignore[import-untyped]

TAG = re.compile(r"\{\{.*?\}\}|\{%.*?%\}")


def render(template: Path, destination: Path, context: Mapping[str, object]) -> None:
    document = DocxTemplate(str(template))
    document.render(dict(context), autoescape=True)
    document.save(str(destination))


def strip_tags(text: str) -> str:
    """差込タグを外した文字列（タグ以外を変えていないかの確かめに使う）。"""
    return TAG.sub("", text)
