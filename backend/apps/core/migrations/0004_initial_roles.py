"""初期ロール（管理者・支援担当者）と権限表（第4章）。

既存の superuser は管理者ロールを付けた通常ユーザーに移す（0005 で superuser を禁止するため）。
"""

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

ROLES = {
    "admin": (
        "管理者",
        ["admin.access", "users.manage", "organization.view", "organization.manage"],
    ),
    "support_staff": ("支援担当者", ["admin.access", "organization.view"]),
}


def add_roles(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    Role = apps.get_model("core", "Role")
    RolePermission = apps.get_model("core", "RolePermission")
    for code, (name, actions) in ROLES.items():
        role, _ = Role.objects.get_or_create(code=code, defaults={"name": name, "data_scope": "all"})
        for action in actions:
            RolePermission.objects.get_or_create(role=role, action=action)

    User = apps.get_model("core", "User")
    UserRole = apps.get_model("core", "UserRole")
    admin_role = Role.objects.get(code="admin")
    for user in User.objects.filter(is_superuser=True):
        UserRole.objects.get_or_create(user=user, role=admin_role)
        user.is_superuser = False
        user.save(update_fields=["is_superuser"])


def remove_roles(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    Role = apps.get_model("core", "Role")
    Role.objects.filter(code__in=ROLES).delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0003_roles")]

    operations = [migrations.RunPython(add_roles, remove_roles)]
