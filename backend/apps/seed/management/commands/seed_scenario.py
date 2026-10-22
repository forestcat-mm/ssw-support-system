"""手動のテストシナリオの本線（王 麗娜さん）の記録を作る（開発用。apps/seed/scenario.py）。

- --until N: 節 1〜N が終わった状態（1 自機関と担当者 … 8 定期面談と相談。既定は 8）。
- --with-docs: 節 6 の生成物と署名済みの添付など、PDF の変換を伴うものも作る（遅い）。
- --ledger: 段階4の台帳の項目（節 1〜3 の一括申請の項目）も入れる。
- --bulk: 一括申請（節 6 の 6-18〜6-20。認定・2026-07-10）も節 5 の後に作る（--ledger を含む。
  --until 5 以上）。Excel は作らない。
- 作成済みなら何もしない。--reset でシナリオの記録だけを消して作り直す（ダミーデータは消さない）。
- seed_dummy の後に流す（担当者 dummy_tanto・自機関を使う）。
- DEBUG かつ ALLOW_DUMMY_DATA のときだけ動く。
- --replace（DEBUG のときだけ）: 台帳の外の、本線と同じ名前の受入れ機関（さくら台フーズ株式会社）・
  外国人（WANG LINA）と、紐づく記録を消してから作る。消す前に件数を種類ごとに出して確かめる
  （--yes で確かめを省く）。消すのと作るのは 1 つのトランザクションで、作れなければ消さない。
  監査ログとほかのデータは消さない。
"""

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

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
            "--ledger",
            action="store_true",
            help="Also fill the ledger items for the bulk application (phase 4).",
        )
        parser.add_argument(
            "--bulk",
            action="store_true",
            help="Also create the bulk application (certificate of eligibility). Implies --ledger.",
        )
        parser.add_argument(
            "--reset", action="store_true", help="Delete the scenario records and recreate them."
        )
        parser.add_argument(
            "--replace",
            action="store_true",
            help="Delete the same host organization and foreign worker made on the screens, with "
            "their records, before creating the scenario (development only).",
        )
        parser.add_argument(
            "--yes", action="store_true", help="Do not ask before deleting with --replace."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["replace"] and not settings.DEBUG:
            raise CommandError("--replace is only for development (DEBUG=True).")
        try:
            with transaction.atomic():
                self.create(options)
        except (builders.SeedNotAllowedError, scenario.ScenarioError) as error:
            raise CommandError(str(error)) from error

    def replace(self, options: dict[str, Any]) -> bool:
        """台帳の外の同じ記録を、件数を見せて確かめてから消す。やめたら False。"""
        rows = scenario.replacement_rows()
        counts = scenario.replacement_counts(rows)
        if not counts:
            self.stdout.write("No records made on the screens to replace.")
            return True
        self.stdout.write("Records made on the screens to delete (by type):")
        for label, count in counts.items():
            self.stdout.write(f"- {label}: {count}")
        if not options["yes"]:
            answer = input("Delete these records and create the scenario? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                self.stdout.write("Cancelled. Nothing was deleted.")
                return False
        deleted = scenario.replace_existing(rows)
        self.stdout.write(self.style.WARNING(f"deleted records made on the screens: {deleted}"))
        return True

    def create(self, options: dict[str, Any]) -> None:
        builders.ensure_allowed()
        if options["replace"] and not self.replace(options):
            return
        if options["reset"]:
            self.stdout.write(f"deleted scenario rows: {scenario.reset()}")
        elif scenario.exists():
            self.stdout.write(
                self.style.WARNING(
                    "The scenario records already exist. Run with --reset to recreate them."
                )
            )
            return
        result = scenario.run(
            options["until"],
            with_docs=options["with_docs"],
            ledger=options["ledger"],
            bulk=options["bulk"],
        )
        # 最後の説明は日本語（開発用の道具。2026-10-20 の指示）
        sections = "、".join(f"{n} {scenario.SECTIONS[n]}" for n in range(1, result.until + 1))
        docs = "あり" if result.with_docs else "なし"
        ledger = "あり" if result.ledger else "なし"
        bulk = "あり" if result.bulk else "なし"
        self.stdout.write(
            self.style.SUCCESS(
                f"テストシナリオの記録を作りました（節: {sections}。生成物: {docs}。"
                f"段階4の台帳の項目: {ledger}。一括申請: {bulk}）: "
                f"{result.rows} 行、生成物 {result.documents} 件"
            )
        )
        if result.not_stored:
            self.stdout.write("値はあるが、今の作りに欄が無い・決まった形で入れられないもの:")
            for line in result.not_stored:
                self.stdout.write(f"- {line}")
        if result.filled:
            self.stdout.write("テストシナリオに値が無いが、入力が必須なので決めて入れたもの:")
            for line in result.filled:
                self.stdout.write(f"- {line}")
