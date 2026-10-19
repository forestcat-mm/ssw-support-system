"""キャリアアッププランの版の「使った日時」と、習熟の目安の年数の数字（修正 1 回目 J1・J6、ADR-0029）。

J1: 版に used_at を足し、既存の生成物（キャリアアッププランの様式）のスナップショットの版の ID と、
    キャリアアッププランの交付記録から入れる（データの追加だけ）。交付記録は、交付日に適用されていた版を
    移行では正確に探さず、その受入れ機関のプランの版のうち、交付日までに適用が始まった版と最も古い版を
    すべて使ったものとする（多めに固定する。固定は今までの扱いと同じなので、失うものは無い）。
J6: 年数の文字（years）を years_legacy に名前を変えて残し、years_from・years_to を足す。「1〜2年目」
    「1～2年目」「1年目から2年目まで」「5年目以降」「3年目」の形を読み取って入れ、読み取れた行は元の文字を
    空にする。読み取れない行は元の文字を残す（画面と生成前の検査で、数字で入れ直すように知らせる）。
    版の行は固定の行だが、移行では DB を直接書き、監査ログに移行として 1 行ずつ残す。
"""

import json
import re
import unicodedata
from typing import Any

from django.db import migrations, models
from django.utils import timezone

_RANGE = re.compile(r"^(\d+)(?:年目)?\s*(?:[〜～~\-－ー]|から)\s*(\d+)年目(?:まで)?$")
_ONWARD = re.compile(r"^(\d+)年目\s*(?:以降|以上|〜|～|~|から)$")
_SINGLE = re.compile(r"^(\d+)年目$")


def parse_years(text: str) -> tuple[int, int | None] | None:
    """年数の文字を (始め, 終わり) に読む（終わりが None なら「以降」）。読めなければ None。"""
    value = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))
    if match := _RANGE.match(value):
        start, end = int(match[1]), int(match[2])
        return (start, end) if 1 <= start <= end else None
    if match := _ONWARD.match(value):
        return (int(match[1]), None) if int(match[1]) >= 1 else None
    if match := _SINGLE.match(value):
        return (int(match[1]), int(match[1])) if int(match[1]) >= 1 else None
    return None


def _audit(apps: Any, model: str, pk: int, label: str, changes: dict[str, Any]) -> None:
    content_type = apps.get_model("contenttypes", "ContentType").objects.filter(
        app_label="hosts", model=model
    ).first()
    apps.get_model("core", "AuditLog").objects.create(
        occurred_at=timezone.now(),
        action="update",
        content_type=content_type,
        object_id=str(pk),
        object_repr=label[:255],
        changes=changes,
        context={"automatic": True, "migration": "hosts.0009"},
    )


def read_years(apps: Any, schema_editor: Any) -> None:
    level_model = apps.get_model("hosts", "CareerPlanLevel")
    for level in level_model.objects.all():
        parsed = parse_years(level.years_legacy)
        if parsed is None:
            continue
        start, end = parsed
        level_model.objects.filter(pk=level.pk).update(
            years_from=start, years_to=end, years_legacy=""
        )
        _audit(
            apps,
            "careerplanlevel",
            level.pk,
            level.name,
            {
                "years_from": [None, start],
                "years_to": [None, end],
                "years_legacy": [level.years_legacy, ""],
            },
        )


def mark_used(apps: Any, schema_editor: Any) -> None:
    version_model = apps.get_model("hosts", "CareerPlanVersion")
    document_model = apps.get_model("forms_engine", "GeneratedDocument")
    delivery_model = apps.get_model("support", "DocumentDelivery")
    used: dict[int, Any] = {}

    def use(pk: int, at: Any) -> None:
        if pk not in used or at < used[pk]:
            used[pk] = at

    for document in document_model.objects.filter(template__form_id="career-plan"):
        try:
            data = json.loads(document.snapshot or "{}").get("data", {})
        except ValueError:
            continue
        pk = data.get("p.plan_version_id")
        if isinstance(pk, int):
            use(pk, document.created_at)
    deliveries = delivery_model.objects.filter(
        kind="career_up_plan", contract__isnull=False
    ).select_related("contract")
    for delivery in deliveries:
        versions = version_model.objects.filter(
            plan__host_organization_id=delivery.contract.host_organization_id
        )
        oldest = {}
        for version in versions.order_by("plan_id", "version"):
            oldest.setdefault(version.plan_id, version.pk)
            if version.applies_from <= delivery.delivered_on:
                use(version.pk, delivery.created_at)
        for pk in oldest.values():
            use(pk, delivery.created_at)
    for version in version_model.objects.filter(pk__in=used):
        version_model.objects.filter(pk=version.pk).update(used_at=used[version.pk])
        _audit(
            apps,
            "careerplanversion",
            version.pk,
            f"v{version.version}",
            {"used_at": [None, used[version.pk].isoformat()]},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("hosts", "0008_support_contract_fees"),
        ("forms_engine", "0004_signed_copies"),
        ("support", "0012_briefing_interpreter_kind_and_more"),
        ("core", "0033_attachment_examined_on"),
    ]

    operations = [
        migrations.RenameField(
            model_name="careerplanlevel", old_name="years", new_name="years_legacy"
        ),
        migrations.AlterField(
            model_name="careerplanlevel",
            name="years_legacy",
            field=models.CharField(
                blank=True, max_length=50, verbose_name="years as a guide (old text)"
            ),
        ),
        migrations.AddField(
            model_name="careerplanlevel",
            name="years_from",
            field=models.PositiveSmallIntegerField(blank=True, null=True, verbose_name="from year"),
        ),
        migrations.AddField(
            model_name="careerplanlevel",
            name="years_to",
            field=models.PositiveSmallIntegerField(blank=True, null=True, verbose_name="to year"),
        ),
        migrations.AddField(
            model_name="careerplanversion",
            name="used_at",
            field=models.DateTimeField(
                blank=True, editable=False, null=True, verbose_name="first used at"
            ),
        ),
        migrations.RunPython(read_years, migrations.RunPython.noop),
        migrations.RunPython(mark_used, migrations.RunPython.noop),
    ]
