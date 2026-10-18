"""帳票の生成前の検査の結果を表示する（生成はしない。T30、ADR-0019）。

例: python manage.py check_form 1-17 --worker 15 [--basis 2026-10-18]
見本の対象とデータ（services/form_samples.py）で検査し、生成不可・警告と直す画面の行き先を出す。
"""

import datetime
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from apps.forms_engine.management.commands.generate_sample_form import findings_text
from apps.workers.models import ForeignWorker
from services import form_generation, form_samples


class Command(BaseCommand):
    help = "Show the pre-generation check of a sample form (1-17 or 5-9) for a foreign worker."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("form_id", choices=["1-17", "5-9"])
        parser.add_argument("--worker", type=int, required=True)
        parser.add_argument("--basis", type=datetime.date.fromisoformat, default=None)

    def handle(self, *args: Any, **options: Any) -> None:
        worker = ForeignWorker.objects.filter(pk=options["worker"]).first()
        if worker is None:
            raise CommandError("foreign worker not found")
        target, data = form_samples.sample(options["form_id"], worker)
        result = form_generation.check(
            options["form_id"],
            target=target,
            data=data,
            basis_on=options["basis"] or timezone.localdate(),
        )
        if not result.findings:
            self.stdout.write("no findings")
            return
        verdict = "cannot generate" if result.blocking else "can generate with warnings"
        self.stdout.write(f"{verdict}:\n{findings_text(result.findings)}")
