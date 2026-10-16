"""外国人の無効化の権限（T13e）。管理者のみ。"""

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

PERMISSIONS = {"admin": ["workers.deactivate"]}


def add(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    Role = apps.get_model("core", "Role")
    RolePermission = apps.get_model("core", "RolePermission")
    for role_code, actions in PERMISSIONS.items():
        for role in Role.objects.filter(code=role_code):
            for action in actions:
                RolePermission.objects.get_or_create(role=role, action=action)


def remove(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    apps.get_model("core", "RolePermission").objects.filter(action="workers.deactivate").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("workers", "0003_worker_deactivation"),
        ("core", "0018_worker_deactivation_actions"),
    ]

    operations = [migrations.RunPython(add, remove)]
