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
from tests.hosts.factories import make_host
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
    # 台帳に同じ年度のほかの外国人の契約があるだけでは判定しない（印の提案だけ。T42-D25）
    make_contract(make_worker(name_romaji="OTHER"), site=contract.versions.get().site)
    outline = submission_bundles.outline(overview, admin)
    entry = next(e for e in outline.entries if e.row_id == "chg_t2_2-3-1")
    assert host_documents.Issue.TABLE2_NOT_NEEDED not in entry.warnings
    assert outline.accepting_suggested and not outline.skip_table2
    # 受入れ機関の印で判定する
    host.accepts_ssw_workers = True
    host.accepts_ssw_workers_since = datetime.date(2026, 4, 1)
    host.save()
    overview = application_sets.overview(contract.worker, "chg", BASIS)
    outline = submission_bundles.outline(overview, admin)
    entry = next(e for e in outline.entries if e.row_id == "chg_t2_2-3-1")
    assert host_documents.Issue.TABLE2_NOT_NEEDED in entry.warnings
    assert outline.skip_table2
    assert outline.default_marks(True)["chg_t2_2-3-1"] == ""


def test_host_form_suggests_the_mark_from_the_ledger() -> None:
    """台帳の雇用契約（始まっているもの）から、印の初期値を提案する（保存は人が確かめて）。"""
    client = verified_client(make_user("tanto42d", roles=("support_staff",)))
    empty = make_host(name="契約の無い会社", corporate_number="", entity_type="sole_proprietor")
    form = client.get(f"/hosts/{empty.pk}/edit/").context["form"]
    assert not form.initial.get("accepts_ssw_workers")
    contract = make_case()
    host = contract.host_organization
    form = client.get(f"/hosts/{host.pk}/edit/").context["form"]
    assert form.initial["accepts_ssw_workers"] is True
    assert form.initial["accepts_ssw_workers_since"] == contract.current_starts_on
    host.refresh_from_db()
    assert host.accepts_ssw_workers is False  # 保存するまでは変わらない


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


def test_rows_omitted_for_accepting_hosts_follow_the_mark() -> None:
    """「同一年度内に既に受け入れている機関…は省略可」の△の行は、受入れ継続の印で判定する。

    印が無い: 報酬に関する説明書・賃金規程の写し・雇用の経緯に係る説明書は要る。雇用の経緯に係る
    説明書は T54 から作れる（生成の画面へ）。職業紹介の画面の写しは、あっせんの有無を判断しない。
    システムで作れない書類の△の行は「要る（アップロード）」と警告する。
    印が有る: どれも当てはまらない。
    """
    contract = make_case()
    host = contract.host_organization
    overview = application_sets.overview(contract.worker, "coe", BASIS)
    rows = by_id(overview)
    for row_id in ("coe_t1-5-1", "coe_t1-5-2", "coe_t1-8-1"):
        assert rows[row_id].applicable is True, row_id
    assert rows["coe_t1-8-1"].status == application_sets.Status.NOT_GENERATED
    assert rows["coe_t1-8-1"].action_url.endswith(f"/contracts/{contract.pk}/documents/1-16/")
    assert rows["coe_t1-8-2"].applicable is None
    assert rows["coe_t1-8-2"].status == application_sets.Status.OUTSIDE
    # システムで作れない書類の△の行（印が無ければ要る）は「要る（アップロード）」
    row = dict(rows["coe_t1-8-1"].row) | {"how": {"out": True}}
    state = application_sets.state_of(row, overview.facts)
    assert state.status == application_sets.Status.UPLOAD_REQUIRED
    assert state.tone == "warning" and not state.done
    host.accepts_ssw_workers = True
    host.accepts_ssw_workers_since = datetime.date(2026, 4, 1)
    host.save()
    omitted = application_sets.overview(contract.worker, "coe", BASIS)
    not_applicable = {state.row["id"] for state in omitted.not_applicable}
    assert {"coe_t1-5-1", "coe_t1-5-2", "coe_t1-8-1", "coe_t1-8-2"} <= not_applicable
