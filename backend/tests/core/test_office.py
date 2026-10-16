import pytest
from django.db.models import ProtectedError

from .factories import make_office, make_staff

pytestmark = pytest.mark.django_db


def test_create_office() -> None:
    office = make_office(name="大阪事務所")
    assert str(office) == "大阪事務所"
    assert office.is_active is True


def test_office_with_staff_cannot_be_deleted() -> None:
    office = make_office()
    make_staff(office=office)
    with pytest.raises(ProtectedError):
        office.delete()
