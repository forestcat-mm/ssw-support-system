"""支援言語の初期値（日本語・中国語（簡体字））。"""

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

INITIAL_LANGUAGES = [
    ("ja", "日本語", 10),
    ("zh-hans", "中国語（簡体字）", 20),
]


def add_languages(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    SupportLanguage = apps.get_model("core", "SupportLanguage")
    for code, name, sort_order in INITIAL_LANGUAGES:
        SupportLanguage.objects.get_or_create(
            code=code, defaults={"name": name, "sort_order": sort_order}
        )


def remove_languages(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    SupportLanguage = apps.get_model("core", "SupportLanguage")
    SupportLanguage.objects.filter(code__in=[code for code, _, _ in INITIAL_LANGUAGES]).delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]

    operations = [migrations.RunPython(add_languages, remove_languages)]
