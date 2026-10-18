"""様式マスタを読み込む（make forms）。何度実行しても同じ結果で、旧版は消さない（ADR-0018）。"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from services import form_templates


class Command(BaseCommand):
    help = "Load form templates and mappings from templates/forms and mappings."

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            report = form_templates.load_all()
        except form_templates.FormTemplateError as error:
            raise CommandError(str(error)) from error
        for key in report.created:
            self.stdout.write(f"created: {key}")
        for key in report.updated:
            self.stdout.write(f"updated: {key}")
        self.stdout.write(
            self.style.SUCCESS(
                f"created {len(report.created)}, updated {len(report.updated)}, "
                f"unchanged {len(report.unchanged)}"
            )
        )
