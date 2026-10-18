"""見本の帳票を生成する（開発者が基盤を確かめるため。T29、ADR-0018）。

例: python manage.py generate_sample_form 1-17 --worker 1
生成履歴の ID・状態・頁数を表示する。PDF の取り出しは認可つきのダウンロードの画面から。
"""

import datetime
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from apps.workers.models import ForeignWorker
from services import form_generation, form_samples


class Command(BaseCommand):
    help = "Generate a sample form (1-17 or 5-9) for a foreign worker."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("form_id", choices=["1-17", "5-9"])
        parser.add_argument("--worker", type=int, required=True)
        parser.add_argument("--basis", type=datetime.date.fromisoformat, default=None)

    def handle(self, *args: Any, **options: Any) -> None:
        worker = ForeignWorker.objects.filter(pk=options["worker"]).first()
        if worker is None:
            raise CommandError("foreign worker not found")
        document = form_generation.request(
            options["form_id"],
            target=worker,
            data=form_samples.sample_data(options["form_id"], worker),
            basis_on=options["basis"] or timezone.localdate(),
            by=None,
        )
        document.refresh_from_db()
        self.stdout.write(
            f"generated document #{document.pk}: {document.status} "
            f"pages={document.page_count} {document.failure_reason}"
        )
