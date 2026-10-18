"""協力確認書の期限のタスクを取り消す（ADR-0015、T28a）。

協力確認書には有効期限が無い（運用要領 本体 印刷 80〜81頁）。日次の起票は、これまで協力確認書の添付の
有効期限から「加入証明・協力確認書の更新」のタスクを起票していた。その未完了（未着手・対応中）の
タスクを、理由つきで取消にし、監査ログに「届出タスクの状態変更」を 1 件ずつ残す。
"""

from typing import Any

from django.db import migrations, models
from django.utils import timezone

REASON = (
    "協力確認書には有効期限が無いため、期限のタスクを取り消した"
    "（ADR-0015。T28a の移行で自動）"
)


def cancel_tasks(apps: Any, schema_editor: Any) -> None:
    content_type_model = apps.get_model("contenttypes", "ContentType")
    attachment_model = apps.get_model("core", "Attachment")
    filing_model = apps.get_model("filings", "Filing")
    audit_model = apps.get_model("core", "AuditLog")
    confirmation_type = content_type_model.objects.filter(
        app_label="hosts", model="hostcooperationconfirmation"
    ).first()
    attachment_type = content_type_model.objects.filter(
        app_label="core", model="attachment"
    ).first()
    filing_type = content_type_model.objects.filter(app_label="filings", model="filing").first()
    if confirmation_type is None or attachment_type is None:
        return
    attachment_ids = list(
        attachment_model.objects.filter(content_type=confirmation_type).values_list(
            "pk", flat=True
        )
    )
    tasks = filing_model.objects.filter(
        kind="membership_renewal",
        content_type=attachment_type,
        object_id__in=attachment_ids,
        status__in=["not_started", "in_progress"],
    )
    for task in tasks:
        before = task.status
        task.status = "cancelled"
        task.status_reason = REASON
        task.save(update_fields=["status", "status_reason", "updated_at"])
        audit_model.objects.create(
            occurred_at=timezone.now(),
            action="filing_transition",
            content_type=filing_type,
            object_id=str(task.pk),
            object_repr=task.target_repr[:255],
            changes={"status": [before, "cancelled"], "status_reason": ["", REASON]},
            context={"automatic": True, "migration": "filings.0007", "reason": REASON},
        )


class Migration(migrations.Migration):

    dependencies = [
        ('filings', '0006_unique_unfinished_tasks'),
        ('hosts', '0003_cooperation_confirmation_per_site'),
        ('core', '0026_password_reset'),
    ]

    operations = [
        migrations.AlterField(
            model_name='filing',
            name='kind',
            field=models.CharField(choices=[('residence_renewal', 'application for extension of period of stay'), ('contract_change', 'notification of change of employment contract'), ('contract_termination', 'notification of termination of employment contract'), ('contract_new', 'notification of conclusion of a new employment contract'), ('acceptance_difficulty', 'notification of difficulty in acceptance'), ('support_plan_change', 'notification of change of support plan'), ('missing', 'notification of missing worker, etc.'), ('support_difficulty', 'report of a special incident in implementing the support plan (form 4-3)'), ('periodic_host', 'periodic notification (host organization)'), ('periodic_support_organization', 'periodic notification (registered support organization)'), ('registration_renewal', 'renewal of registration (own organization)'), ('membership_renewal', 'renewal of membership certificate'), ('passport_check', 'check of passport renewal'), ('support_contract_renewal', 'renewal of support contract'), ('contract_expiry_check', 'check of employment contract expiry (termination not registered)'), ('periodic_interview', 'periodic interview (every 3 months)')], max_length=40, verbose_name='kind'),
        ),
        migrations.RunPython(cancel_tasks, migrations.RunPython.noop),
    ]
