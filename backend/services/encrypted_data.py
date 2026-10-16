"""暗号化列を持つ全モデルへの一括処理（鍵のローテーション・ブラインドインデックスの作り直し）。

行の書き換えはシグナルを通さない（値の意味は変わらないため一般の更新として記録しない）。
代わりに、実施したこと自体を件数付きで監査ログに 1 件残す（CLAUDE.md の規約）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.apps import apps
from django.db import connection, models, transaction

from apps.core.fields import BlindIndexField, EncryptedCharField
from apps.core.models import AuditAction
from services import audit, crypto


@dataclass
class Report:
    # "app_label.model.field" -> 件数
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def encrypted_columns() -> list[tuple[type[models.Model], EncryptedCharField]]:
    return [
        (model, model_field)
        for model in apps.get_models()
        for model_field in model._meta.concrete_fields
        if isinstance(model_field, EncryptedCharField)
    ]


def blind_index_columns() -> list[tuple[type[models.Model], BlindIndexField]]:
    return [
        (model, model_field)
        for model in apps.get_models()
        for model_field in model._meta.concrete_fields
        if isinstance(model_field, BlindIndexField)
    ]


def _raw_tokens(model: type[models.Model], column: str) -> list[tuple[Any, str]]:
    """暗号文のまま読む（ORM だと from_db_value で復号されるため）。"""
    table = connection.ops.quote_name(model._meta.db_table)
    pk = connection.ops.quote_name(model._meta.pk.column)
    col = connection.ops.quote_name(column)
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {pk}, {col} FROM {table} WHERE {col} IS NOT NULL AND {col} <> ''")
        return [(row[0], row[1]) for row in cursor.fetchall()]


def count_rows_not_on_primary_key() -> Report:
    report = Report()
    for model, model_field in encrypted_columns():
        stale = [
            pk
            for pk, token in _raw_tokens(model, model_field.column)
            if not crypto.is_encrypted_with_primary_key(token)
        ]
        report.counts[f"{model._meta.label_lower}.{model_field.name}"] = len(stale)
    return report


@transaction.atomic
def rotate_encryption_keys() -> Report:
    """古い鍵の暗号文を、先頭の鍵で暗号化し直す。"""
    report = Report()
    for model, model_field in encrypted_columns():
        table = connection.ops.quote_name(model._meta.db_table)
        pk_col = connection.ops.quote_name(model._meta.pk.column)
        col = connection.ops.quote_name(model_field.column)
        rotated = 0
        with connection.cursor() as cursor:
            for pk, token in _raw_tokens(model, model_field.column):
                if crypto.is_encrypted_with_primary_key(token):
                    continue
                cursor.execute(
                    f"UPDATE {table} SET {col} = %s WHERE {pk_col} = %s",
                    [crypto.reencrypt(token), pk],
                )
                rotated += 1
        report.counts[f"{model._meta.label_lower}.{model_field.name}"] = rotated
    audit.record(
        AuditAction.ENCRYPTION_KEY_ROTATION,
        context={"rotated": report.counts, "keys_configured": len(crypto.encryption_keys())},
        actor=None,
    )
    return report


@transaction.atomic
def rebuild_blind_indexes() -> Report:
    """現在の BLIND_INDEX_KEY で検索用の値を作り直す（鍵の差し替え後に実行する）。"""
    report = Report()
    for model, model_field in blind_index_columns():
        changed = 0
        with audit.suppressed():
            for instance in model._base_manager.all().iterator():
                value = crypto.blind_index(getattr(instance, model_field.source))
                if getattr(instance, model_field.attname) != value:
                    model._base_manager.filter(pk=instance.pk).update(
                        **{model_field.attname: value}
                    )
                    changed += 1
        report.counts[f"{model._meta.label_lower}.{model_field.name}"] = changed
    audit.record(AuditAction.BLIND_INDEX_REBUILD, context={"changed": report.counts}, actor=None)
    return report
