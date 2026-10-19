"""入国前の在留情報の扱い（手動のテストシナリオの本線で分かった直し）。

- 在留資格認定証明書の行（在留カードの番号なし）は入国前の扱い（期限は在留期限ではない。B5）。
  認定証明書で入国して在留カードの番号がある行は在留期限として扱う（一覧・詳細）。
- 生成前の検査: 入国前の外国人は、在留資格認定証明書の交付申請のときに在留情報が無くても止めない。
  在留中なのに在留情報が無いときは、今までどおり止める。
"""

from datetime import date

import pytest

from apps.workers.models import ApplicationType, StatusOfResidence, WorkerStatus
from services import form_checks, worker_detail
from services.form_checks import CheckContext
from tests.workers.factories import add_residence, make_worker

pytestmark = pytest.mark.django_db

BASIS = date(2026, 7, 10)


def residence_issues(worker: object) -> list[str]:
    check = form_checks.CHECKS["residence_valid_on_basis"].function
    context = CheckContext(worker=worker, target=worker, basis_on=BASIS, data={})  # type: ignore[arg-type]
    return [issue.code for issue in check(context, {})]


def test_card_issued_on_entry_is_a_residence_expiry() -> None:
    worker = make_worker(status=WorkerStatus.PRE_ENTRY)
    certificate = add_residence(
        worker,
        card_number="",
        status_of_residence=StatusOfResidence.SSW_1,
        application_type=ApplicationType.CERTIFICATE_OF_ELIGIBILITY,
        permitted_on=date(2026, 8, 20),
        expires_on=date(2026, 11, 19),
    )
    card = add_residence(
        worker,
        card_number="TK48261937EA",
        status_of_residence=StatusOfResidence.SSW_1,
        application_type=ApplicationType.CERTIFICATE_OF_ELIGIBILITY,
        permitted_on=date(2026, 9, 15),
        expires_on=date(2027, 9, 15),
    )
    assert worker_detail.is_certificate_of_eligibility(certificate)
    assert not worker_detail.is_certificate_of_eligibility(card)


def test_pre_entry_worker_without_residence_is_not_blocked() -> None:
    assert residence_issues(make_worker(status=WorkerStatus.PRE_ENTRY)) == []
    assert residence_issues(make_worker(status=WorkerStatus.RESIDING)) == ["residence_missing"]
