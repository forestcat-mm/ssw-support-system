"""自機関（1 行）の取得。"""

from apps.core.models import Organization


class OrganizationNotConfiguredError(Exception):
    """自機関がまだ登録されていない。"""


DEFAULT_DISPLAY_NAME = "SSW Support"


def organization_display_name() -> str:
    """画面・メール・認証アプリに出す自機関名（未登録時は既定名）。"""
    try:
        return get_organization().name
    except OrganizationNotConfiguredError:
        return DEFAULT_DISPLAY_NAME


def get_organization() -> Organization:
    organization = Organization.objects.first()
    if organization is None:
        raise OrganizationNotConfiguredError("Organization has not been registered yet.")
    return organization
