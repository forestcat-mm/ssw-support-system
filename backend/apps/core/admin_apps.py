from django.contrib.admin.apps import AdminConfig


class SupportAdminConfig(AdminConfig):
    """django.contrib.admin の既定サイトをロール判定の SupportAdminSite に差し替える。"""

    default_site = "apps.core.admin_site.SupportAdminSite"
