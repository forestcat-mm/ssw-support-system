"""届出タスクの権限と、日次の起票の実行予定（Django-Q2、毎日 6:00 Asia/Tokyo。ADR-0005）。"""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

SCHEDULE_NAME = "daily_filings"
PERMISSIONS = {
    "admin": ["filings.view", "filings.manage", "system.manage"],
    "support_staff": ["filings.view", "filings.manage"],
}


def add(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    Role = apps.get_model("core", "Role")
    RolePermission = apps.get_model("core", "RolePermission")
    for role_code, actions in PERMISSIONS.items():
        for role in Role.objects.filter(code=role_code):
            for action in actions:
                RolePermission.objects.get_or_create(role=role, action=action)

    Schedule = apps.get_model("django_q", "Schedule")
    tokyo = ZoneInfo("Asia/Tokyo")
    now = datetime.now(tokyo)
    first = datetime.combine(now.date(), time(6, 0), tzinfo=tokyo)
    if first <= now:
        first += timedelta(days=1)
    Schedule.objects.get_or_create(
        name=SCHEDULE_NAME,
        defaults={
            "func": "apps.filings.tasks.create_scheduled_filings",
            "schedule_type": "D",  # 毎日
            "repeats": -1,
            "next_run": first,
        },
    )


def remove(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    apps.get_model("django_q", "Schedule").objects.filter(name=SCHEDULE_NAME).delete()
    apps.get_model("core", "RolePermission").objects.filter(
        action__in=["filings.view", "filings.manage", "system.manage"]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("filings", "0001_initial"),
        ("core", "0017_filing_actions"),
        ("django_q", "0019_alter_task_options_alter_ormq_key_alter_ormq_lock_and_more"),
    ]

    operations = [migrations.RunPython(add, remove)]
