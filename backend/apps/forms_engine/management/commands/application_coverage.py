"""申請の書類の一覧の全部の行の対応を Markdown で書く（T42 の追加。開発用）。"""

from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.utils import timezone

from services import application_coverage


class Command(BaseCommand):
    help = "Write the coverage of the rows of the documents for the application (Markdown)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--output", default="", help="File to write (default: stdout).")

    def handle(self, *args: Any, **options: Any) -> None:
        text = application_coverage.markdown(timezone.localdate())
        if options["output"]:
            Path(options["output"]).write_text(text, encoding="utf-8")
        else:
            self.stdout.write(text)
