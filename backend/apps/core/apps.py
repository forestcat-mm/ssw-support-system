from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    label = "core"
    verbose_name = _("Organization and users")

    def ready(self) -> None:
        from apps.core import checks  # noqa: F401  システムチェックの登録
        from services import audit

        audit.connect_signals()
