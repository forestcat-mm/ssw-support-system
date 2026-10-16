"""BLIND_INDEX_KEY を差し替えた後に、検索用の値を作り直す（作り直すまで検索は当たらない）。"""

from typing import Any

from django.core.management.base import BaseCommand

from services import encrypted_data


class Command(BaseCommand):
    help = "Recompute all blind index columns with the current BLIND_INDEX_KEY."

    def handle(self, *args: Any, **options: Any) -> None:
        report = encrypted_data.rebuild_blind_indexes()
        for column, count in sorted(report.counts.items()):
            self.stdout.write(f"{column}: {count}")
        self.stdout.write(self.style.SUCCESS(f"rows updated: {report.total}"))
