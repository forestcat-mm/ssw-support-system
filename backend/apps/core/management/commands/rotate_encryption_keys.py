"""暗号化鍵のローテーション。

1. FIELD_ENCRYPTION_KEYS の先頭に新しい鍵を足して起動する（旧鍵は後ろに残す）
2. manage.py rotate_encryption_keys（全行を新しい鍵で暗号化し直す。監査ログに 1 件）
3. manage.py rotate_encryption_keys --check が 0 件になったら旧鍵を外す
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from services import encrypted_data


class Command(BaseCommand):
    help = "Re-encrypt all encrypted columns with the first key in FIELD_ENCRYPTION_KEYS."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--check",
            action="store_true",
            help="Only count rows not yet encrypted with the first key.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["check"]:
            report = encrypted_data.count_rows_not_on_primary_key()
            label = "rows on old keys"
        else:
            report = encrypted_data.rotate_encryption_keys()
            label = "rows re-encrypted"
        for column, count in sorted(report.counts.items()):
            self.stdout.write(f"{column}: {count}")
        self.stdout.write(self.style.SUCCESS(f"{label}: {report.total}"))
