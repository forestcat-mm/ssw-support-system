"""手動のテストシナリオの本線（王 麗娜さん）の記録を作る（開発用。apps/seed/scenario.py）。

- --until N: 節 1〜N が終わった状態（1 自機関と担当者 … 8 定期面談と相談。既定は 8）。
- --with-docs: 節 6 の生成物と署名済みの添付など、PDF の変換を伴うものも作る（遅い）。
- 作成済みなら何もしない。--reset でシナリオの記録だけを消して作り直す（ダミーデータは消さない）。
- seed_dummy の後に流す（担当者 dummy_tanto・自機関を使う）。
- DEBUG かつ ALLOW_DUMMY_DATA のときだけ動く。
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.seed import builders, scenario


class Command(BaseCommand):
    help = "Create the main line of the manual test scenario (WANG LINA) by section."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--until",
            type=int,
            default=scenario.LAST_SECTION,
            help=f"Create the state after sections 1 to N (1-{scenario.LAST_SECTION}).",
        )
        parser.add_argument(
            "--with-docs",
            action="store_true",
            help="Also generate the forms and attach the signed copies (slow).",
        )
        parser.add_argument(
            "--reset", action="store_true", help="Delete the scenario records and recreate them."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            builders.ensure_allowed()
            if options["reset"]:
                self.stdout.write(f"deleted scenario rows: {scenario.reset()}")
            elif scenario.exists():
                self.stdout.write(
                    self.style.WARNING(
                        "The scenario records already exist. Run with --reset to recreate them."
                    )
                )
                return
            result = scenario.run(options["until"], with_docs=options["with_docs"])
        except (builders.SeedNotAllowedError, scenario.ScenarioError) as error:
            raise CommandError(str(error)) from error
        sections = ", ".join(f"{n} {scenario.SECTIONS[n]}" for n in range(1, result.until + 1))
        self.stdout.write(
            self.style.SUCCESS(
                f"Created the scenario (sections {sections}; with docs: {result.with_docs}): "
                f"{result.rows} rows, {result.documents} documents"
            )
        )
        self.stdout.write("Values not stored (no field in the current design):")
        for line in result.not_stored:
            self.stdout.write(f"- {line}")
        self.stdout.write("Values filled because the field is required:")
        for line in result.filled:
            self.stdout.write(f"- {line}")
