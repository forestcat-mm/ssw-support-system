"""日付の到来で起票する届出タスクを作る（日次。Django-Q2 からも同じ処理を呼ぶ）。

何度実行しても同じ件数になる（同じ種別×対象×起算日は 1 件）。--date で基準日を指定できる。
"""

from datetime import date
from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.utils import timezone

from services import filings


class Command(BaseCommand):
    help = "Create filing tasks whose opening date has arrived (idempotent)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--date", type=date.fromisoformat, help="Base date (YYYY-MM-DD).")

    def handle(self, *args: Any, **options: Any) -> None:
        today: date = options["date"] or timezone.localdate()
        result = filings.create_scheduled(today)
        for filing in result.created:
            self.stdout.write(f"created: {filing}")
        self.stdout.write(
            self.style.SUCCESS(
                f"{today}: created {len(result.created)}, already existed {result.existing}"
            )
        )
