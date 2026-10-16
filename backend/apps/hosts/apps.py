from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class HostsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.hosts"
    label = "hosts"
    verbose_name = _("Host organizations")
