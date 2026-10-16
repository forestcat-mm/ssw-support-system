"""国・地域の初期データと、外国人台帳の権限（管理者・支援担当者の両方）。

国名は入管の様式の「国籍・地域」の表記に合わせる。ほかの国は admin から追加する。
"""

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

COUNTRIES = [
    ("CN", "中国"),
    ("VN", "ベトナム"),
    ("ID", "インドネシア"),
    ("PH", "フィリピン"),
    ("MM", "ミャンマー"),
    ("NP", "ネパール"),
    ("TH", "タイ"),
    ("KH", "カンボジア"),
    ("MN", "モンゴル"),
    ("LK", "スリランカ"),
    ("IN", "インド"),
    ("BD", "バングラデシュ"),
    ("UZ", "ウズベキスタン"),
    ("LA", "ラオス"),
    ("PK", "パキスタン"),
    ("KR", "韓国"),
    ("TW", "台湾"),
    ("JP", "日本"),
]

WORKER_ACTIONS = ["workers.view", "workers.manage"]


def add(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    Country = apps.get_model("core", "Country")
    for order, (code, name) in enumerate(COUNTRIES, start=1):
        Country.objects.get_or_create(code=code, defaults={"name": name, "sort_order": order * 10})
    Role = apps.get_model("core", "Role")
    RolePermission = apps.get_model("core", "RolePermission")
    for role in Role.objects.filter(code__in=["admin", "support_staff"]):
        for action in WORKER_ACTIONS:
            RolePermission.objects.get_or_create(role=role, action=action)


def remove(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    apps.get_model("core", "RolePermission").objects.filter(action__in=WORKER_ACTIONS).delete()
    apps.get_model("core", "Country").objects.filter(code__in=[c for c, _ in COUNTRIES]).delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0013_country")]

    operations = [migrations.RunPython(add, remove)]
