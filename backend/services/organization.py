"""自機関（1 行）の取得。"""

from apps.core.models import Organization


class OrganizationNotConfiguredError(Exception):
    """自機関がまだ登録されていない。"""


def get_organization() -> Organization:
    organization = Organization.objects.first()
    if organization is None:
        raise OrganizationNotConfiguredError("Organization has not been registered yet.")
    return organization
