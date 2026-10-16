"""監査ログの閲覧（audit.view）を管理者ロールに付ける（第4章: 監査ログ閲覧は管理者）。"""

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def add(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    Role = apps.get_model("core", "Role")
    RolePermission = apps.get_model("core", "RolePermission")
    for role in Role.objects.filter(code="admin"):
        RolePermission.objects.get_or_create(role=role, action="audit.view")


def remove(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    RolePermission = apps.get_model("core", "RolePermission")
    RolePermission.objects.filter(action="audit.view").delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0009_auditlog_append_only_trigger")]

    operations = [migrations.RunPython(add, remove)]
