import pytest
from django.test import Client

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "model", ["user", "organization", "office", "staff", "supportlanguage", "attachment"]
)
def test_changelist_is_served(admin_client: Client, model: str) -> None:
    assert admin_client.get(f"/admin/core/{model}/").status_code == 200


@pytest.mark.parametrize("model", ["user", "organization", "office", "staff", "supportlanguage"])
def test_add_form_is_served(admin_client: Client, model: str) -> None:
    assert admin_client.get(f"/admin/core/{model}/add/").status_code == 200
