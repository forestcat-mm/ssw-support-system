"""要配慮の添付（健康診断の書類）を全員分ダウンロードする権限（T23、ADR-0011）。管理者のみ。"""

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

PERMISSIONS = {"admin": ["workers.sensitive_documents"]}


def add(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    Role = apps.get_model("core", "Role")
    RolePermission = apps.get_model("core", "RolePermission")
    for role_code, actions in PERMISSIONS.items():
        for role in Role.objects.filter(code=role_code):
            for action in actions:
                RolePermission.objects.get_or_create(role=role, action=action)


def remove(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    apps.get_model("core", "RolePermission").objects.filter(action="workers.sensitive_documents").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("workers", "0004_deactivate_permission"),
        ("core", "0025_sensitive_attachments"),
    ]

    operations = [migrations.RunPython(add, remove)]
