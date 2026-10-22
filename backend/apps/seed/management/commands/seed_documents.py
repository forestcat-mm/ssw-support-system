"""ダミーの生成物の場面を作る（開発用。T39-D2）。seed_dummy の後に実行する。

署名待ち（日中併記）・署名済み・署名済み書類の回収のタスク・警告つきの生成物を作る（PDF の変換を
伴う）。作成済みなら何もしない。DEBUG かつ ALLOW_DUMMY_DATA=true のときだけ動く。
内容は docs/dummy-data.md を参照。

基準日は、--date があればその日、無ければ seed_dummy が記録した基準日（fix2）。記録の無い古い
開発用 DB では今日にし、そのことを知らせる。生成と署名は基準日の時刻で行う（seed_scenario と同じ）。
"""

from datetime import date
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from apps.seed import builders, document_builders
from apps.seed.context import as_of


class Command(BaseCommand):
    help = "Create dummy generated documents (awaiting signature, signed, with warnings)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--date",
            type=date.fromisoformat,
            help="Base date (YYYY-MM-DD). Defaults to the base date of seed_dummy.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            builders.ensure_allowed()
            if document_builders.exists():
                self.stdout.write(self.style.WARNING("Dummy documents already exist."))
                return
            base = options["date"] or builders.base_date()
            if base is None:
                base = timezone.localdate()
                # 説明は日本語（開発用の道具。2026-10-20 の指示）
                self.stdout.write(
                    self.style.WARNING(
                        f"seed_dummy の基準日が記録されていないので、今日（{base}）で作ります。"
                        "seed の基準日で作るときは make seed-docs DATE=YYYY-MM-DD で"
                        "渡してください。"
                    )
                )
            with as_of(base):
                result = document_builders.run(base)
        except (builders.SeedNotAllowedError, document_builders.DocumentsNotAllowedError) as error:
            raise CommandError(str(error)) from error
        statuses = [f"{d.template.form_id}:{d.status}" for d in result.documents]
        self.stdout.write(
            self.style.SUCCESS(
                f"Created dummy documents (base date {base}): {statuses}, "
                f"collection tasks: {result.tasks}"
            )
        )
