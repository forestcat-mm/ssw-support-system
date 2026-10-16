import pytest
from django.db import IntegrityError, transaction
from django.test import Client

from apps.core.models import Organization
from services.organization import OrganizationNotConfiguredError, get_organization

from .factories import make_organization

pytestmark = pytest.mark.django_db


def test_second_row_is_rejected_by_database() -> None:
    make_organization()
    with pytest.raises(IntegrityError), transaction.atomic():
        make_organization(name="2つ目の機関")
    assert Organization.objects.count() == 1


def test_admin_hides_add_once_registered(admin_client: Client) -> None:
    changelist = admin_client.get("/admin/core/organization/")
    assert changelist.context["has_add_permission"] is True

    make_organization()

    changelist = admin_client.get("/admin/core/organization/")
    assert changelist.context["has_add_permission"] is False
    assert admin_client.get("/admin/core/organization/add/").status_code == 403


def test_admin_cannot_delete(admin_client: Client) -> None:
    organization = make_organization()
    response = admin_client.get(f"/admin/core/organization/{organization.pk}/delete/")
    assert response.status_code == 403


def test_get_organization() -> None:
    with pytest.raises(OrganizationNotConfiguredError):
        get_organization()
    organization = make_organization()
    assert get_organization() == organization
