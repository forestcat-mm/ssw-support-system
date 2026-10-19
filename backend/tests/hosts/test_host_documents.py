"""受入れ機関・事業所の書類の保管庫（T42 の追加）。

- 協定書・年間カレンダーは事業所ごと・対象期間ごと、第2表の書類は発行日（原本は 3 か月以内）。
- 申請の書類の一覧と統合 PDF は、事業所と基準日から有効なものを選び、無ければ最新のものに警告。
- 版の根拠資料（雇用契約の版の添付）はそのまま使える。
"""

import datetime
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.contracts.models import VariableHoursUnit
from apps.core.models import AttachmentKind, AuditAction, AuditLog, SupportLanguage
from apps.hosts.models import HostDocument, HostDocumentKind
from apps.seed.context import DUMMY_PDF
from services import application_sets, host_documents, submission_bundles
from tests.contracts.factories import make_contract
from tests.core.factories import make_user, verified_client
from tests.forms_engine.test_form_application import make_case
from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db

BASIS = datetime.date(2026, 10, 18)
K = HostDocumentKind


def pdf(name: str = "doc.pdf") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, DUMMY_PDF, content_type="application/pdf")


def keep(host: Any, kind: str, *, languages: tuple[str, ...] = (), **values: Any) -> HostDocument:
    document = HostDocument(host_organization=host, kind=kind, **values)
    ids = [SupportLanguage.objects.get(code=code).pk for code in languages]
    return host_documents.add(document, [pdf()], language_ids=ids, by=None)


def by_id(overview: application_sets.Overview) -> dict[str, application_sets.RowState]:
    return {state.row["id"]: state for state in overview.rows}


def one_year_case() -> Any:
    return make_case(variable_hours_unit=VariableHoursUnit.ONE_YEAR)


def test_calendar_and_agreement_are_chosen_by_site_and_period() -> None:
    contract = one_year_case()
    host = contract.host_organization
    site = contract.versions.get().site
    keep(
        host,
        K.ANNUAL_CALENDAR,
        site=site,
        period_start=datetime.date(2025, 4, 1),
        period_end=datetime.date(2026, 3, 31),
        languages=("ja", "zh-hans"),
    )  # 期間の外
    current = keep(
        host,
        K.ANNUAL_CALENDAR,
        site=site,
        period_start=datetime.date(2026, 4, 1),
        period_end=datetime.date(2027, 3, 31),
        languages=("ja", "zh-hans"),
    )
    keep(
        host,
        K.VARIABLE_HOURS_AGREEMENT,
        site=site,
        period_start=datetime.date(2025, 4, 1),
        period_end=datetime.date(2026, 3, 31),
    )
    found = by_id(application_sets.overview(contract.worker, "chg", BASIS))
    calendar = found["chg_t1-6-3"]
    assert calendar.status == application_sets.Status.ATTACHED
    assert calendar.host_document == current and calendar.issues == ()
    agreement = found["chg_t1-6-4"]
    assert agreement.status == application_sets.Status.CHECK_DOCUMENT
    assert agreement.issues == (host_documents.Issue.OUT_OF_PERIOD,)


def test_calendar_language_must_include_the_workers_language() -> None:
    contract = one_year_case()
    host = contract.host_organization
    keep(
        host,
        K.ANNUAL_CALENDAR,
        site=contract.versions.get().site,
        period_start=datetime.date(2026, 4, 1),
        period_end=datetime.date(2027, 3, 31),
        languages=("ja",),
    )
    state = by_id(application_sets.overview(contract.worker, "chg", BASIS))["chg_t1-6-3"]
    assert state.issues == (host_documents.Issue.CALENDAR_LANGUAGE,)
    assert "併記" in state.issue_texts[0]


def test_table2_certificates_need_a_recent_issue_date() -> None:
    contract = make_case()
    host = contract.host_organization
    keep(host, K.REGISTRY_CERTIFICATE, issued_on=datetime.date(2026, 6, 1))
    keep(host, K.LOCAL_TAX, issued_on=datetime.date(2026, 9, 1))
    found = by_id(application_sets.overview(contract.worker, "chg", BASIS))
    assert found["chg_t2_2-3-1"].issues == (host_documents.Issue.ISSUED_LONG_AGO,)
    assert found["chg_t2_2-9-1"].status == application_sets.Status.ATTACHED
    # 申請人の住民税の行（第1表）には第2表の書類を綴じない
    assert found["chg_t1-9-1"].host_document is None


def test_version_attachments_are_still_used() -> None:
    """版の根拠資料があれば、問題のある保管庫の書類より版の添付を使う（移し替えない）。"""
    from services.attachments import create_attachment

    contract = one_year_case()
    version = contract.versions.get()
    create_attachment(
        target=version,
        kind=AttachmentKind.VARIABLE_HOURS_DOCUMENTS,
        file=pdf(),
        created_by=None,
    )
    keep(
        contract.host_organization,
        K.ANNUAL_CALENDAR,
        site=version.site,
        period_start=datetime.date(2025, 4, 1),
        period_end=datetime.date(2026, 3, 31),
        languages=("ja", "zh-hans"),
    )
    state = by_id(application_sets.overview(contract.worker, "chg", BASIS))["chg_t1-6-3"]
    assert state.status == application_sets.Status.ATTACHED and state.host_document is None


def test_bundle_binds_host_documents_and_warns_about_table2() -> None:
    contract = make_case()
    host = contract.host_organization
    keep(host, K.REGISTRY_CERTIFICATE, issued_on=datetime.date(2026, 9, 1))
    admin = make_user("admin42b", roles=("admin",))
    overview = application_sets.overview(contract.worker, "chg", BASIS)
    outline = submission_bundles.outline(overview, admin)
    entry = next(e for e in outline.entries if e.row_id == "chg_t2_2-3-1")
    assert [p.source for p in entry.bound] == [submission_bundles.Source.HOST_DOCUMENT]
    assert host_documents.Issue.TABLE2_NOT_NEEDED not in entry.warnings
    # 同じ年度にほかの外国人を受け入れている機関
    make_contract(make_worker(name_romaji="OTHER"), site=contract.versions.get().site)
    outline = submission_bundles.outline(overview, admin)
    entry = next(e for e in outline.entries if e.row_id == "chg_t2_2-3-1")
    assert host_documents.Issue.TABLE2_NOT_NEEDED in entry.warnings
    assert outline.skip_table2
    assert outline.default_marks(True)["chg_t2_2-3-1"] == ""


def test_screens_add_and_void_with_audit() -> None:
    contract = one_year_case()
    host = contract.host_organization
    site = contract.versions.get().site
    client = verified_client(make_user("tanto42b", roles=("support_staff",)))
    url = f"/hosts/{host.pk}/documents/new/"
    page = client.get(url)
    assert page.status_code == 200
    data = {
        page.context["once_field"]: page.context["once_token"],
        "kind": K.ANNUAL_CALENDAR,
        "site": site.pk,
        "uploads": pdf(),
    }
    response = client.post(url, data)
    assert response.status_code == 200  # 対象期間が要る
    assert "period_end" in response.context["form"].errors
    page = client.get(url)
    data |= {
        page.context["once_field"]: page.context["once_token"],
        "period_start": "2026-04-01",
        "period_end": "2027-03-31",
        "languages": [
            SupportLanguage.objects.get(code="ja").pk,
            SupportLanguage.objects.get(code="zh-hans").pk,
        ],
        "uploads": pdf(),
    }
    assert client.post(url, data).status_code == 302
    document = HostDocument.objects.get()
    assert AuditLog.objects.filter(
        action=AuditAction.CREATE, content_type__model="hostdocument", object_id=str(document.pk)
    ).exists()
    detail = client.get(f"/hosts/{host.pk}/").content.decode()
    assert f'data-host-document="{document.pk}"' in detail
    void_url = f"/hosts/{host.pk}/documents/{document.pk}/void/"
    page = client.get(void_url)
    response = client.post(
        void_url,
        {page.context["once_field"]: page.context["once_token"], "reason": "別の事業所の書類"},
    )
    assert response.status_code == 302
    document.refresh_from_db()
    assert document.voided_at is not None
    assert not host_documents.files_of(document)
    viewer = verified_client(make_user("viewer42b", roles=()))
    assert viewer.get(url).status_code == 403
