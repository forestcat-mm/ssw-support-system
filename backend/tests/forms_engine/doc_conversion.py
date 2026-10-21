"""配布が .doc の様式を変換し直すテストの共通部分（第1-4号・第1-25号。T34-D1、ADR-0023）。

converted.docx は、manifest の converter に書いた版の LibreOffice（手元の web のコンテナ）で作った。
版が違う LibreOffice（クラウドの VM の 24.2 など）では出力の XML が変わるので、比べずに飛ばす
（理由に両方の版を出す。make checks のログに写る。手元で流す。docs/cloud-session.md）。
"""

import json
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest
from django.conf import settings

TOOLS = Path(settings.FORM_TEMPLATES_ROOT).parent.parent / "tools" / "forms"
VERSION = re.compile(r"LibreOffice (\d+(?:\.\d+)+)")


def convert_doc_or_skip(version_folder: Path) -> ModuleType:
    """tools/forms/convert_doc.py。LibreOffice の版が converted.docx を作った版と違えば飛ばす。"""
    sys.path.insert(0, str(TOOLS))
    import convert_doc  # type: ignore[import-not-found]

    manifest = json.loads((version_folder / "manifest.json").read_text("utf-8"))
    made = VERSION.search(manifest["converter"])
    here = VERSION.search(convert_doc.version())
    assert made, manifest["converter"]
    if here is None or here.group(1) != made.group(1):
        pytest.skip(
            f"LibreOffice の版が converted.docx を作った版と違う（作った版 {made.group(1)}、"
            f"この環境 {here.group(1) if here else '無い'}）。手元の web のコンテナで流す"
        )
    module: ModuleType = convert_doc
    return module
