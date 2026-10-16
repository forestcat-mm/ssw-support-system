import pytest
from django.db import IntegrityError, transaction

from apps.core.models import EmploymentType, SupportLanguage, User

from .factories import make_office, make_staff

pytestmark = pytest.mark.django_db


def test_staff_without_login_user() -> None:
    staff = make_staff(user=None)
    assert staff.user is None


def test_one_user_links_to_at_most_one_staff() -> None:
    user = User.objects.create_user(username="tanto")
    office = make_office()
    make_staff(office=office, user=user)
    with pytest.raises(IntegrityError), transaction.atomic():
        make_staff(office=office, name="別の担当者", user=user)


def test_staff_must_have_a_role() -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        make_staff(is_support_manager=False, is_support_staff=False)


def test_staff_can_hold_both_roles_and_languages() -> None:
    staff = make_staff(
        is_support_manager=True,
        is_support_staff=True,
        employment_type=EmploymentType.PART_TIME,
        has_life_consultation_experience=True,
        life_consultation_experience_note="2024年4月〜2026年3月 外国人生活相談窓口",
    )
    staff.languages.set(SupportLanguage.objects.filter(code__in=["ja", "zh-hans"]))
    assert set(staff.languages.values_list("code", flat=True)) == {"ja", "zh-hans"}
