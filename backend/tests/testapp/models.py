from django.db import models

from apps.core.fields import BlindIndexField, EncryptedCharField


class SecretRecord(models.Model):
    """在留カード番号（検索あり）と口座番号（検索なし）を模したテスト用モデル。"""

    card_number = EncryptedCharField(max_length=12, blank=True)
    card_number_index = BlindIndexField(source="card_number")
    bank_account = EncryptedCharField(max_length=20, null=True, blank=True)
    note = models.CharField(max_length=50, blank=True)

    def __str__(self) -> str:
        return f"SecretRecord {self.pk}"
