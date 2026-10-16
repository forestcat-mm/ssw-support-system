"""MFA の緊急リセット（サーバー上で実行する）。

- reset_mfa <username>: 管理者自身が締め出され、他に管理者がいないとき
- reset_mfa --all --yes: DB やバックアップの漏洩が疑われるとき（全員が次回ログインで再設定）
監査ログには実行者なし・via="manage.py reset_mfa" で 1 人 1 件記録される。
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.core.models import User
from services.mfa import reset_mfa

VIA = "manage.py reset_mfa"


class Command(BaseCommand):
    help = "Delete TOTP devices and recovery codes of a user, or of all users (break-glass)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("username", nargs="?")
        parser.add_argument("--all", action="store_true", help="Reset every user.")
        parser.add_argument("--yes", action="store_true", help="Confirm --all.")

    def handle(self, *args: Any, **options: Any) -> None:
        if options["all"]:
            if options["username"]:
                raise CommandError("Give either a username or --all, not both.")
            if not options["yes"]:
                raise CommandError("--all resets every user's MFA. Add --yes to confirm.")
            users = list(User.objects.order_by("pk"))
        elif options["username"]:
            try:
                users = [User.objects.get(username=options["username"])]
            except User.DoesNotExist as error:
                raise CommandError(f"User not found: {options['username']}") from error
        else:
            raise CommandError("Give a username or --all.")
        for user in users:
            reset_mfa(user, by=None, via=VIA)
            self.stdout.write(f"Reset MFA for {user.username}")
        self.stdout.write(self.style.SUCCESS(f"users reset: {len(users)}"))
