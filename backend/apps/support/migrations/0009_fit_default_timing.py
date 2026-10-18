"""自機関の標準の計画の実施予定の初期値を、第1-17号の欄に収まる書き方に直す（T31 の直し、ADR-0020）。

標準の計画の最新の版の実施予定が、古い初期値のまま（利用者が変えていない）の行だけを、新しい版として
直す（版は上書きしない。制約7）。最新の版の中身（項目・細目・住居・相談・共生施策）をそのまま写し、
該当の行の実施予定の文だけを変える。標準の計画が無い・古い初期値が無いときは何もしない。
"""

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.utils import timezone

OLD_TO_NEW = {
    "1.main": ("在留資格認定証明書交付申請前（在留資格変更許可申請前）", "認定申請前（変更申請前）"),
    "8.a": ("非自発的離職時", "非自発的離職時に適宜実施"),
    "8.b": ("非自発的離職時", "非自発的離職時に適宜実施"),
    "8.c": ("非自発的離職時", "非自発的離職時に適宜実施"),
    "8.e": ("", "非自発的離職時に適宜実施"),
    "8.f": ("非自発的離職時", "非自発的離職時に適宜実施"),
    "8.g": ("", "非自発的離職時に適宜実施"),
}
CHANGE_REASON = "第1-17号の実施予定の欄に収まる書き方・記載例の書き方に直した（ADR-0020）"


def _copy(instance, **values):  # type: ignore[no-untyped-def]
    instance.pk = None
    instance.id = None
    for name, value in values.items():
        setattr(instance, name, value)
    instance._state.adding = True
    instance.save()
    return instance


def fit_default_timing(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    SupportPlan = apps.get_model("support", "SupportPlan")
    plan = SupportPlan.objects.filter(is_template=True).first()
    if plan is None:
        return
    latest = plan.versions.order_by("-version").first()
    if latest is None:
        return
    activities = {row.code: row for row in latest.activities.all()}
    changes = {
        code: new
        for code, (old, new) in OLD_TO_NEW.items()
        if code in activities and activities[code].planned and activities[code].planned_note == old
    }
    if not changes:
        return
    today = timezone.localdate()
    items = list(latest.items.all())
    languages = {item.pk: list(item.languages.all()) for item in items}
    housing = getattr(latest, "housing", None)
    consultation = getattr(latest, "consultation", None)
    checks = list(latest.municipality_checks.all())
    version = _copy(
        latest,
        version=latest.version + 1,
        applies_from=max(today, latest.applies_from),
        change_reason=CHANGE_REASON,
        created_on=today,
    )
    for item in items:
        old_pk = item.pk
        copied = _copy(item, version=version)
        copied.languages.set(languages[old_pk])
    for code, activity in activities.items():
        _copy(activity, version=version, planned_note=changes.get(code, activity.planned_note))
    if housing is not None:
        _copy(housing, version=version)
    if consultation is not None:
        _copy(consultation, version=version)
    for check in checks:
        _copy(check, version=version)


class Migration(migrations.Migration):
    dependencies = [
        ("support", "0008_form_1_17_fields"),
    ]

    operations = [
        migrations.RunPython(fit_default_timing, migrations.RunPython.noop),
    ]
