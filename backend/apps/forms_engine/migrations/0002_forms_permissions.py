"""帳票の権限。生成と PDF のダウンロードは両ロール（T29、ADR-0018）。"""

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

PERMISSIONS = {
    "admin": ["forms.generate"],
    "support_staff": ["forms.generate"],
}


def add(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    Role = apps.get_model("core", "Role")
    RolePermission = apps.get_model("core", "RolePermission")
    for role_code, actions in PERMISSIONS.items():
        for role in Role.objects.filter(code=role_code):
            for action in actions:
                RolePermission.objects.get_or_create(role=role, action=action)


def remove(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    apps.get_model("core", "RolePermission").objects.filter(action="forms.generate").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("forms_engine", "0001_initial"),
        ("core", "0028_alter_rolepermission_action"),
    ]

    operations = [migrations.RunPython(add, remove)]
