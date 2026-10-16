"""管理者ロールのユーザーを作る（createsuperuser の代わり。superuser は使わない）。"""

import getpass
import os
from typing import Any

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser

from services.users import create_admin_user

PASSWORD_ENV = "CREATE_ADMIN_PASSWORD"


class Command(BaseCommand):
    help = "Create a user with the admin role."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--username")
        parser.add_argument("--email", default="")
        parser.add_argument(
            "--noinput",
            action="store_true",
            help=f"Read the password from the {PASSWORD_ENV} environment variable.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        username: str = options["username"] or input("Username: ").strip()
        email: str = options["email"] or ("" if options["noinput"] else input("Email: ").strip())
        if options["noinput"]:
            password = os.environ.get(PASSWORD_ENV, "")
            if not password:
                raise CommandError(f"{PASSWORD_ENV} is not set.")
        else:
            password = getpass.getpass("Password: ")
            if password != getpass.getpass("Password (again): "):
                raise CommandError("Passwords do not match.")
        try:
            user = create_admin_user(username=username, email=email, password=password)
        except ValidationError as error:
            raise CommandError("; ".join(error.messages)) from error
        self.stdout.write(self.style.SUCCESS(f"Created admin user: {user.username}"))
