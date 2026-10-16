from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class SeedConfig(AppConfig):
    """開発用のダミーデータ（seed_dummy）。本番では使わない（DEBUG と ALLOW_DUMMY_DATA で拒否）。"""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.seed"
    label = "seed"
    verbose_name = _("Dummy data (development)")
