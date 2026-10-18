"""見本の帳票を生成する（開発者が基盤を確かめるため。T29・T30、ADR-0018・0019）。

例: python manage.py generate_sample_form 1-17 --worker 1 [--basis 2026-10-18] [--accept-warnings]
生成前の検査で生成不可なら、不足を表示して止める。警告があるときは --accept-warnings が要る。
生成履歴の ID・状態・頁数を表示する。PDF の取り出しは認可つきのダウンロードの画面から。
"""

import datetime
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from apps.workers.models import ForeignWorker
from services import form_checks, form_generation, form_samples


def findings_text(findings: list[form_checks.Finding]) -> str:
    return "\n".join(
        f"  [{finding.severity}] {finding.rule}/{finding.code}: {finding.message}"
        + (f" -> {finding.destination.url()}" if finding.destination else "")
        for finding in findings
    )


class Command(BaseCommand):
    help = "Generate a sample form (1-17, 5-9 or 5-8) for a foreign worker."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("form_id", choices=["1-17", "5-9", "5-8"])
        parser.add_argument("--worker", type=int, required=True)
        parser.add_argument("--basis", type=datetime.date.fromisoformat, default=None)
        parser.add_argument("--accept-warnings", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        worker = ForeignWorker.objects.filter(pk=options["worker"]).first()
        if worker is None:
            raise CommandError("foreign worker not found")
        basis_on = options["basis"] or timezone.localdate()
        target, data = form_samples.sample(options["form_id"], worker, basis_on)
        try:
            document = form_generation.request(
                options["form_id"],
                target=target,
                data=data,
                basis_on=basis_on,
                by=None,
                accept_warnings=options["accept_warnings"],
            )
        except form_generation.GenerationBlocked as blocked:
            raise CommandError(
                "cannot generate:\n" + findings_text(blocked.result.findings)
            ) from blocked
        except form_generation.WarningsNotAccepted as pending:
            raise CommandError(
                "warnings (run again with --accept-warnings):\n"
                + findings_text(pending.result.warnings)
            ) from pending
        document.refresh_from_db()
        self.stdout.write(
            f"generated document #{document.pk}: {document.status} "
            f"pages={document.page_count} warnings={len(document.findings)} "
            f"{document.failure_reason}"
        )
