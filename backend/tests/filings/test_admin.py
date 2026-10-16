"""届出タスクの admin（一覧・絞り込み・状態の変更・起算日の訂正・手動の起票・権限）と、
雇用契約の版の追加画面からの自動起票。"""

import re
from datetime import date
from typing import Any

import pytest
from django.test import Client

from apps.contracts.models import TerminationReason
from apps.core.models import AuditAction, AuditLog
from apps.filings.models import Filing, FilingKind, FilingStatus
from services import contracts
from tests.contracts.factories import make_contract
from tests.contracts.test_admin import VERSION_URL, _inline, _version_form
from tests.core.factories import make_user, verified_client
from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db

URL = "/admin/filings/filing/"


def _errors(response: Any) -> list[str]:
    html = response.content.decode()
    return [
        re.sub(r"<[^>]+>", " ", b).strip()
        for b in re.findall(r'<ul class="errorlist[^"]*"[^>]*>(.*?)</ul>', html, re.S)
    ]


def _terminated() -> Filing:
    contract = make_contract()
    contracts.terminate(contract, on=date(2026, 9, 30), reason=TerminationReason.COMPANY, by=None)
    return Filing.objects.get(kind=FilingKind.CONTRACT_TERMINATION)


def _form(filing: Filing, **values: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "status": filing.status,
        "submitted_on": "",
        "receipt_number": "",
        "status_reason": "",
        "assignee": "",
        "note": "",
        "new_basis_on": "",
        "rebase_reason": "",
    }
    return data | values


def test_list_filters_by_category_and_filer(admin_client: Client) -> None:
    _terminated()
    assert admin_client.get(URL).status_code == 200
    host_filings = admin_client.get(URL, {"filer": "host"}).context["cl"].result_list
    assert {f.kind for f in host_filings} == {"contract_termination", "acceptance_difficulty"}
    assert (
        list(admin_client.get(URL, {"filer": "support_organization"}).context["cl"].result_list)
        == []
    )
    assert (
        len(admin_client.get(URL, {"category": "immigration_filing"}).context["cl"].result_list)
        == 2
    )
    assert (
        "超過" in admin_client.get(URL).content.decode()
        or "日以内" in admin_client.get(URL).content.decode()
    )


def test_status_change_with_required_fields(admin_client: Client, admin_user: Any) -> None:
    filing = _terminated()
    url = f"{URL}{filing.pk}/change/"
    response = admin_client.post(url, _form(filing, status="submitted"))
    assert any("提出日" in e for e in _errors(response))
    response = admin_client.post(url, _form(filing, status="submitted", submitted_on="2026-10-05"))
    assert response.status_code == 302, _errors(response)
    filing.refresh_from_db()
    assert (filing.status, filing.submitted_on) == (FilingStatus.SUBMITTED, date(2026, 10, 5))
    log = AuditLog.objects.filter(action=AuditAction.FILING_TRANSITION).latest("pk")
    assert log.actor == admin_user and log.changes["status"] == ["not_started", "submitted"]

    response = admin_client.post(
        url, _form(filing, status="not_started", submitted_on="2026-10-05")
    )
    assert any("変更することはできません" in e for e in _errors(response))


def test_rebase_from_admin(admin_client: Client) -> None:
    difficulty = (
        Filing.objects.get(kind=FilingKind.ACCEPTANCE_DIFFICULTY) if _terminated() else None
    )
    assert difficulty is not None
    url = f"{URL}{difficulty.pk}/change/"
    response = admin_client.post(url, _form(difficulty, new_basis_on="2026-08-31"))
    assert any("訂正の理由" in e for e in _errors(response))
    response = admin_client.post(
        url, _form(difficulty, new_basis_on="2026-08-31", rebase_reason="解雇予告日")
    )
    assert response.status_code == 302, _errors(response)
    difficulty.refresh_from_db()
    assert difficulty.due_on == date(2026, 9, 14)


def test_manual_filing(admin_client: Client) -> None:
    worker = make_worker()
    data = {
        "kind": "missing",
        "basis_on": "2026-10-16",
        "target_worker": worker.pk,
        "target_contract": "",
        "target_host": "",
        "assignee": "",
        "note": "連絡が取れない",
    }
    response = admin_client.post(f"{URL}add/", data)
    assert response.status_code == 302, _errors(response)
    filing = Filing.objects.get()
    assert (filing.kind, filing.worker, filing.due_on) == (
        FilingKind.MISSING,
        worker,
        date(2026, 10, 30),
    )
    assert filing.trigger == "manual"
    response = admin_client.post(
        f"{URL}add/", data | {"target_host": "", "target_contract": "", "target_worker": ""}
    )
    assert any("対象を 1 つだけ" in e for e in _errors(response))
    page = admin_client.get(f"{URL}add/").content.decode()
    assert (
        "support_difficulty" in page and "residence_renewal" not in page
    )  # 手動で起票できる種別だけ


def test_permissions(support_staff_client: Client) -> None:
    filing = _terminated()
    assert support_staff_client.get(URL).status_code == 200
    assert support_staff_client.get(f"{URL}{filing.pk}/delete/").status_code == 403
    assert support_staff_client.get("/admin/django_q/schedule/").status_code == 403
    assert verified_client(make_user("norole")).get(URL, follow=True).status_code == 403


def test_admin_can_see_schedule(admin_client: Client) -> None:
    page = admin_client.get("/admin/django_q/schedule/")
    assert page.status_code == 200
    assert "daily_filings" in page.content.decode()


def test_wage_decrease_from_version_admin_creates_change_filing(admin_client: Client) -> None:
    contract = make_contract()
    site = contract.versions.get().site
    data = _version_form(site) | {
        "contract_choice": contract.pk,
        "change_reason": "減給",
        "changed_on": "2026-10-01",
        "base_wage": "200000",
    }
    data |= _inline("deductions", [{"kind": "housing", "amount": "30000"}])
    response = admin_client.post(f"{VERSION_URL}add/", data)
    assert response.status_code == 302, _errors(response)
    filing = Filing.objects.get(kind=FilingKind.CONTRACT_CHANGE)
    codes = [f["code"] for f in filing.trigger_details["required"]]
    assert "base_wage_decreased" in codes
    assert filing.basis_on == date(2026, 10, 1)
