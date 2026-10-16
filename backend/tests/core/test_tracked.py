import pytest
from django.test import Client

from apps.core.models import Office, User

from .factories import make_user

pytestmark = pytest.mark.django_db

OFFICE_FORM = {"name": "本社", "postal_code": "100-0001", "address": "東京都千代田区千代田1-1"}


def test_admin_stamps_created_and_updated_by(admin_client: Client, admin_user: User) -> None:
    assert admin_client.post("/admin/core/office/add/", OFFICE_FORM).status_code == 302
    office = Office.objects.get()
    assert office.created_by == admin_user
    assert office.updated_by == admin_user

    editor = make_user("editor", roles=("admin",))
    client = Client()
    client.force_login(editor)
    data = OFFICE_FORM | {"name": "本社（移転後）", "is_active": "on"}
    assert client.post(f"/admin/core/office/{office.pk}/change/", data).status_code == 302
    office.refresh_from_db()
    assert office.created_by == admin_user
    assert office.updated_by == editor
