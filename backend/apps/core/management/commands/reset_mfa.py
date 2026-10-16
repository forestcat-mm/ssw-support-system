"""MFA の緊急リセット（管理者自身が締め出され、他に管理者がいないとき用。サーバー上で実行する）。"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.core.models import User
from services.mfa import reset_mfa


class Command(BaseCommand):
    help = "Delete the TOTP device and recovery codes of a user (break-glass)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("username")

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            user = User.objects.get(username=options["username"])
        except User.DoesNotExist as error:
            raise CommandError(f"User not found: {options['username']}") from error
        # 監査ログには実行者なし・via="manage.py reset_mfa" で記録される
        reset_mfa(user, by=None, via="manage.py reset_mfa")
        self.stdout.write(self.style.SUCCESS(f"Reset MFA for {user.username}"))
