"""配布が .doc の様式を docx に変換する（開発用。T34-D1、ADR-0023）。

入管のファイル original.doc はそのまま版のフォルダに置き、この道具の出力 converted.docx を
template.docx の元にする（converted.docx は手で直さない）。LibreOffice（soffice）で変換するので、
web のコンテナの中で動かす。変換の環境（soffice --version）は manifest に書く。

使い方（リポジトリの直下で）:
    docker compose exec web python ../tools/forms/convert_doc.py \\
        ../templates/forms/1-4/2026-10-18/original.doc ../templates/forms/1-4/2026-10-18/converted.docx
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FILTER = "docx:MS Word 2007 XML"


def convert(source: Path, destination: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="convert-doc-") as workdir:
        work = Path(workdir)
        copy = work / "converted.doc"
        shutil.copyfile(source, copy)
        command = [
            "soffice",
            "--headless",
            "--norestore",
            "--nolockcheck",
            f"-env:UserInstallation=file://{work / 'profile'}",
            "--convert-to",
            FILTER,
            "--outdir",
            str(work),
            str(copy),
        ]
        result = subprocess.run(command, capture_output=True, timeout=120, check=False)
        output = work / "converted.docx"
        if result.returncode != 0 or not output.is_file():
            raise SystemExit(f"soffice failed (exit code {result.returncode})")
        shutil.copyfile(output, destination)


def version() -> str:
    result = subprocess.run(["soffice", "--version"], capture_output=True, text=True, check=False)
    return result.stdout.strip()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: convert_doc.py <original.doc> <converted.docx>")
    convert(Path(sys.argv[1]), Path(sys.argv[2]))
    print(version())
