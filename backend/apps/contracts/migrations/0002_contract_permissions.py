"""雇用契約台帳の権限。閲覧・登録は両ロール、終了の取り消しは管理者のみ。"""

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

PERMISSIONS = {
    "admin": ["contracts.view", "contracts.manage", "contracts.reopen"],
    "support_staff": ["contracts.view", "contracts.manage"],
}


def add(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    Role = apps.get_model("core", "Role")
    RolePermission = apps.get_model("core", "RolePermission")
    for role_code, actions in PERMISSIONS.items():
        for role in Role.objects.filter(code=role_code):
            for action in actions:
                RolePermission.objects.get_or_create(role=role, action=action)


def remove(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    apps.get_model("core", "RolePermission").objects.filter(
        action__in=["contracts.view", "contracts.manage", "contracts.reopen"]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("contracts", "0001_initial"),
        ("core", "0016_contract_actions_and_attachment_kinds"),
    ]

    operations = [migrations.RunPython(add, remove)]
