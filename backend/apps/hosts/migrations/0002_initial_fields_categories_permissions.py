"""分野・業務区分の初期データ（仕様書第6章）と、受入れ機関台帳・マスタ編集の権限。

業務区分の開始日が空 = 制度上の開始日を定めない（現行）。履歴が必要になった時点で告示日を入れる。
飲食料品製造業分野は 2027-04-01 に「飲食料品製造業全般」から 2 区分に分かれる。
"""

from datetime import date

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

FIELDS = [
    {
        "code": "industrial_products",
        "name": "工業製品製造業分野",
        "competent_ministry": "経済産業省",
        "membership_type": "registered_corporation",
        "membership_body_name": "特定技能外国人受入事業実施法人（登録法人）",
        "support_org_membership_required": False,
        "sort_order": 10,
    },
    {
        "code": "food_manufacturing",
        "name": "飲食料品製造業分野",
        "competent_ministry": "農林水産省（水産加工業は水産庁）",
        "membership_type": "council",
        "membership_body_name": "食品産業特定技能協議会",
        "support_org_membership_required": True,
        "sort_order": 20,
    },
]

INDUSTRIAL_CATEGORIES = [
    ("machine_metal_processing", "機械金属加工"),
    ("electrical_electronic_assembly", "電気電子機器組立て"),
    ("metal_surface_treatment", "金属表面処理"),
    ("paper_container_cardboard", "紙器・段ボール箱製造"),
    ("concrete_products", "コンクリート製品製造"),
    ("rpf", "RPF製造"),
    ("ceramic_products", "陶磁器製品製造"),
    ("printing_binding", "印刷・製本"),
    ("textile_products", "紡織製品製造"),
    ("sewing", "縫製"),
    ("electric_wire_cable", "電線・ケーブル製造"),
    ("prefab_housing_products", "プレハブ住宅製品製造"),
    ("furniture", "家具製造"),
    ("refractories", "定形・不定形耐火物製造"),
    ("ready_mixed_concrete", "生コンクリート製造"),
    ("rubber_products", "ゴム製品製造"),
    ("bags", "かばん製造"),
]

FOOD_CATEGORIES = [
    ("food_manufacturing_general", "飲食料品製造業全般", None, date(2027, 3, 31)),
    ("food_manufacturing", "飲食料品製造業", date(2027, 4, 1), None),
    ("fishery_processing", "水産加工業", date(2027, 4, 1), None),
]

PERMISSIONS = {
    "admin": ["hosts.view", "hosts.manage", "masters.manage"],
    "support_staff": ["hosts.view", "hosts.manage"],
}


def add(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    Field = apps.get_model("hosts", "SpecifiedSkillField")
    Category = apps.get_model("hosts", "OperationCategory")
    fields = {}
    for values in FIELDS:
        code = values["code"]
        fields[code], _ = Field.objects.get_or_create(
            code=code, defaults={k: v for k, v in values.items() if k != "code"}
        )
    for order, (code, name) in enumerate(INDUSTRIAL_CATEGORIES, start=1):
        Category.objects.get_or_create(
            code=code,
            defaults={"field": fields["industrial_products"], "name": name, "sort_order": order * 10},
        )
    for order, (code, name, valid_from, valid_until) in enumerate(FOOD_CATEGORIES, start=1):
        Category.objects.get_or_create(
            code=code,
            defaults={
                "field": fields["food_manufacturing"],
                "name": name,
                "valid_from": valid_from,
                "valid_until": valid_until,
                "sort_order": order * 10,
            },
        )
    Role = apps.get_model("core", "Role")
    RolePermission = apps.get_model("core", "RolePermission")
    for role_code, actions in PERMISSIONS.items():
        for role in Role.objects.filter(code=role_code):
            for action in actions:
                RolePermission.objects.get_or_create(role=role, action=action)


def remove(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    apps.get_model("core", "RolePermission").objects.filter(
        action__in=["hosts.view", "hosts.manage", "masters.manage"]
    ).delete()
    apps.get_model("hosts", "OperationCategory").objects.all().delete()
    apps.get_model("hosts", "SpecifiedSkillField").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("hosts", "0001_initial"),
        ("core", "0015_host_actions_and_attachment_kinds"),
    ]

    operations = [migrations.RunPython(add, remove)]
