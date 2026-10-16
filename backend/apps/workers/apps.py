from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class WorkersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.workers"
    label = "workers"
    verbose_name = _("Foreign workers")
