"""最新行の参照ビュー（第7章「最新行を参照ビューで取る」）。取り消し済みの行は除く。

外国人 ID と行 ID だけを返すので、元のテーブルに列を足してもビューを作り直す必要はない。
"""

from django.db import migrations

VIEWS = {
    "workers_latest_residence": ("workers_residencerecord", "permitted_on"),
    "workers_latest_passport": ("workers_passportrecord", "issued_on"),
    "workers_latest_address": ("workers_workeraddress", "effective_from"),
    "workers_latest_contact": ("workers_workercontact", "effective_from"),
}


def _create(view: str, table: str, order_by: str) -> str:
    return (
        f"CREATE VIEW {view} AS "
        f"SELECT DISTINCT ON (worker_id) worker_id, id AS record_id FROM {table} "
        f"WHERE voided_at IS NULL ORDER BY worker_id, {order_by} DESC, id DESC;"
    )


class Migration(migrations.Migration):
    dependencies = [("workers", "0001_initial")]

    operations = [
        migrations.RunSQL(_create(view, table, order_by), f"DROP VIEW IF EXISTS {view};")
        for view, (table, order_by) in VIEWS.items()
    ]
