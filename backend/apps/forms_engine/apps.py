from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class FormsEngineConfig(AppConfig):
    """帳票の基盤（様式マスタ・マッピング定義・生成履歴。段階3の T29、ADR-0018）。"""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.forms_engine"
    label = "forms_engine"
    verbose_name = _("Forms")
