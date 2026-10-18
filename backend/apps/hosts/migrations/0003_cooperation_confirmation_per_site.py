"""協力確認書を「事業所×提出先の市区町村への 1 回の提出」で持つ（ADR-0015、T28a）。

既存の行には事業所が無い。所在地に提出先の市区町村名を含む事業所があればそれを、無ければ
その受入れ機関の最初の事業所を入れる（移行の時点で行があるのは開発環境のダミーデータだけ）。
事業所の無い受入れ機関の行は移せないので、移行を止める。
"""

from typing import Any

import django.db.models.deletion
from django.db import migrations, models


def fill_site(apps: Any, schema_editor: Any) -> None:
    confirmation_model = apps.get_model("hosts", "HostCooperationConfirmation")
    site_model = apps.get_model("hosts", "HostSite")
    for confirmation in confirmation_model.objects.all():
        sites = list(
            site_model.objects.filter(host_organization_id=confirmation.host_organization_id)
            .order_by("pk")
        )
        if not sites:
            raise RuntimeError(
                f"cooperation confirmation #{confirmation.pk} has no site to belong to"
            )
        matched = [site for site in sites if confirmation.municipality in site.address]
        confirmation.site_id = (matched or sites)[0].pk
        confirmation.save(update_fields=["site"])


class Migration(migrations.Migration):
    dependencies = [
        ("hosts", "0002_initial_fields_categories_permissions"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="hostcooperationconfirmation",
            name="hosts_cooperation_confirmation_unique",
        ),
        migrations.AddField(
            model_name="hostcooperationconfirmation",
            name="site",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="cooperation_confirmations",
                to="hosts.hostsite",
                verbose_name="host site",
            ),
        ),
        migrations.RunPython(fill_site, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="hostcooperationconfirmation",
            name="site",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="cooperation_confirmations",
                to="hosts.hostsite",
                verbose_name="host site",
            ),
        ),
        migrations.AlterField(
            model_name="hostcooperationconfirmation",
            name="municipality",
            field=models.CharField(max_length=100, verbose_name="submitted to (municipality)"),
        ),
        migrations.AlterModelOptions(
            name="hostcooperationconfirmation",
            options={
                "ordering": ["host_organization", "site", "municipality", "-submitted_on", "-pk"],
                "verbose_name": "cooperation confirmation",
                "verbose_name_plural": "cooperation confirmations",
            },
        ),
    ]
