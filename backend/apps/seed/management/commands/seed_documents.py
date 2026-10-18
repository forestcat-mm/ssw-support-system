"""ダミーの生成物の場面を作る（開発用。T39-D2）。seed_dummy の後に実行する。

署名待ち（日中併記）・署名済み・署名済み書類の回収のタスク・警告つきの生成物を作る（PDF の変換を
伴う）。作成済みなら何もしない。DEBUG かつ ALLOW_DUMMY_DATA=true のときだけ動く。
内容は docs/dummy-data.md を参照。
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.seed import builders, document_builders


class Command(BaseCommand):
    help = "Create dummy generated documents (awaiting signature, signed, with warnings)."

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            builders.ensure_allowed()
            if document_builders.exists():
                self.stdout.write(self.style.WARNING("Dummy documents already exist."))
                return
            result = document_builders.run(timezone.localdate())
        except (builders.SeedNotAllowedError, document_builders.DocumentsNotAllowedError) as error:
            raise CommandError(str(error)) from error
        statuses = [f"{d.template.form_id}:{d.status}" for d in result.documents]
        self.stdout.write(
            self.style.SUCCESS(
                f"Created dummy documents: {statuses}, collection tasks: {result.tasks}"
            )
        )
