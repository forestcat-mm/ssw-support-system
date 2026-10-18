"""帳票の admin（開発者が様式マスタと生成履歴を見るだけ。追加・変更・削除はしない）。

様式の読み込みは load_form_templates（make forms）。生成物の中身（スナップショット・ファイル）は
admin に出さない（PDF は認可つきのダウンロードだけ。ADR-0018）。
"""

from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from apps.core.admin import TrackedModelAdmin

from .models import FormMapping, FormTemplate, GeneratedDocument


class ReadOnlyAdmin(TrackedModelAdmin[Any]):
    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(FormTemplate)
class FormTemplateAdmin(ReadOnlyAdmin):
    list_display = ("form_id", "name", "valid_from", "language", "method", "revision_unknown")
    list_filter = ("form_id", "method", "language")


@admin.register(FormMapping)
class FormMappingAdmin(ReadOnlyAdmin):
    list_display = ("template", "path", "sha256")


@admin.register(GeneratedDocument)
class GeneratedDocumentAdmin(ReadOnlyAdmin):
    list_display = ("pk", "template", "basis_on", "status", "page_count", "created_at")
    list_filter = ("status", "template__form_id")
    exclude = ("snapshot", "filled_file", "pdf_file")
