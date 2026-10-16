"""ダミーデータを作る（開発用）。外国人 30 人・受入れ機関 2 社・契約・届出タスク。

- 同じ基準日なら同じ内容（乱数の種は固定）。作成済みなら何もしない。--reset で作り直す。
- DEBUG かつ ALLOW_DUMMY_DATA=true のときだけ動く（本番で誤って実行しない）。
内容は docs/dummy-data.md を参照。
"""

from datetime import date
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from apps.seed import builders


class Command(BaseCommand):
    help = "Create dummy data for development (30 foreign workers, 2 host organizations)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--reset", action="store_true", help="Delete dummy data and recreate it."
        )
        parser.add_argument("--date", type=date.fromisoformat, help="Base date (YYYY-MM-DD).")

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            builders.ensure_allowed()
        except builders.SeedNotAllowedError as error:
            raise CommandError(str(error)) from error
        base: date = options["date"] or timezone.localdate()
        if options["reset"]:
            deleted = builders.reset()
            self.stdout.write(f"deleted dummy rows: {deleted}")
        elif builders.seeded_count():
            self.stdout.write(
                self.style.WARNING(
                    f"Dummy data already exists ({builders.seeded_count()} rows). "
                    "Run with --reset to recreate it."
                )
            )
            return
        ctx = builders.run(base)
        counts = {
            "workers": len(ctx.workers),
            "hosts": len(ctx.hosts),
            "sites": len(ctx.sites),
            "staff": len(ctx.staff),
            "rows": len(ctx.created),
        }
        self.stdout.write(self.style.SUCCESS(f"Created dummy data (base date {base}): {counts}"))
