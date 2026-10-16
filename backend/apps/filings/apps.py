from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class FilingsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.filings"
    label = "filings"
    verbose_name = _("Filings and tasks")
