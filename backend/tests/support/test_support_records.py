"""支援実績の記録のサービス（T18）: 検査・計画の版の自動判定・取消・添付の取消・集計。"""

from datetime import date, time
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile

from apps.core.models import AttachmentKind, AuditAction, AuditLog, SupportLanguage
from apps.support.models import BodyLanguage, Counterpart, RecordMethod, SupportRecord
from services import attachments, filings, support_plans, support_records, workers
from services.support_catalog import SupportItem
from services.support_records import RecordInput
from tests.conftest import PDF_BYTES
from tests.contracts.factories import make_contract, site_with_categories
from tests.core.factories import make_staff
from tests.hosts.factories import VALID_NUMBERS, make_host, make_site
from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 17)


@pytest.fixture
def worker() -> Any:
    staff = make_staff(name="佐藤")
    worker = make_worker(assigned_staff=staff)
    make_contract(worker, site=site_with_categories())  # 2026-04-01〜2027-03-31
    return worker


def transfer(worker: Any, **overrides: Any) -> RecordInput:
    values: dict[str, Any] = {
        "kind": SupportItem.AIRPORT_TRANSFER,
        "occurred_on": date(2026, 4, 1),
        "method": RecordMethod.IN_PERSON,
        "staff": worker.assigned_staff,
        "body": "成田空港で出迎え、社宅まで送迎した。",
        "details": {"direction": "2.a", "airport": "成田国際空港", "transport": "社用車"},
    }
    return RecordInput(**(values | overrides))


def error_keys(error: pytest.ExceptionInfo[ValidationError]) -> set[str]:
    return set(error.value.error_dict)


def test_create_record_with_details_and_attachment(worker: Any) -> None:
    file = ContentFile(PDF_BYTES, name="送迎確認書.pdf")
    record = support_records.create_record(
        worker,
        transfer(worker, languages=list(SupportLanguage.objects.filter(code="zh-hans"))),
        by=None,
        files=[file],
        today=TODAY,
    )
    assert record.details == {"direction": "2.a", "airport": "成田国際空港", "transport": "社用車"}
    assert [lang.code for lang in record.languages.all()] == ["zh-hans"]
    attachment = record.attachments.get()
    assert attachment.kind == AttachmentKind.SUPPORT_RECORD
    assert attachment.original_filename == "送迎確認書.pdf"
    assert AuditLog.objects.filter(
        action=AuditAction.CREATE, content_type__model="supportrecord", object_id=str(record.pk)
    ).exists()


def test_validate_common_fields(worker: Any) -> None:
    data = transfer(
        worker,
        occurred_on=date(2026, 10, 18),
        staff=None,
        body=" ",
        meeting_url="https://example.com/meet",
        counterpart_name="上司の名前",
        duration_minutes=0,
    )
    with pytest.raises(ValidationError) as error:
        support_records.create_record(worker, data, by=None, today=TODAY)
    assert error_keys(error) == {
        "occurred_on",
        "staff",
        "body",
        "meeting_url",
        "counterpart_name",
        "duration_minutes",
    }
    assert not SupportRecord.objects.exists()


def test_supervisor_name_and_online_url_are_allowed(worker: Any) -> None:
    data = transfer(
        worker,
        kind=SupportItem.EXCHANGE,
        method=RecordMethod.ONLINE,
        meeting_url="https://example.com/meet",
        counterpart=Counterpart.SUPERVISOR,
        counterpart_name="工場長 田中",
        start_time=time(10, 0),
        duration_minutes=30,
        details={"event": "地域の交流会"},
    )
    record = support_records.create_record(worker, data, by=None, today=TODAY)
    assert (record.counterpart_name, record.duration_minutes) == ("工場長 田中", 30)


ALL_METHODS = ("in_person", "online", "phone", "video", "message")
METHODS = {
    SupportItem.AIRPORT_TRANSFER: {"in_person"},
    SupportItem.PUBLIC_PROCEDURES: {"in_person"},
    SupportItem.HOUSING_AND_CONTRACTS: {"in_person", "online", "phone"},
    SupportItem.JAPANESE_LEARNING: {"in_person", "online", "phone"},
    SupportItem.EXCHANGE: {"in_person", "online", "phone"},
    SupportItem.JOB_CHANGE: {"in_person", "online", "phone"},
}


@pytest.mark.parametrize("kind", list(METHODS))
@pytest.mark.parametrize("method", ALL_METHODS)
def test_methods_are_limited_per_kind(worker: Any, kind: SupportItem, method: str) -> None:
    """送迎・公的手続への同行は対面だけ。ほかは対面・オンライン・電話（ADR-0014）。"""
    assert set(support_records.methods_for(kind)) == METHODS[kind]
    data = transfer(worker, kind=kind, method=method, details={})
    try:
        support_records.validate(worker, data, today=TODAY)
    except ValidationError as error:
        codes = {e.code for e in error.error_dict.get("method", [])}
    else:
        codes = set()
    assert codes == (set() if method in METHODS[kind] else {"method_for_kind"})


@pytest.mark.parametrize(
    ("kind", "details", "keys"),
    [
        (
            SupportItem.AIRPORT_TRANSFER,
            {"direction": "2.c"},
            {"details.direction", "details.airport"},
        ),
        (SupportItem.HOUSING_AND_CONTRACTS, {}, {"details.activities"}),
        (
            SupportItem.HOUSING_AND_CONTRACTS,
            {"activities": ["3a.a"], "property_type": "rented"},
            {"details.property_type"},
        ),
        (SupportItem.PUBLIC_PROCEDURES, {"office": "市役所"}, {"details.procedure"}),
        (SupportItem.JAPANESE_LEARNING, {"ways": ["9.z"]}, {"details.ways"}),
        (SupportItem.EXCHANGE, {"event": "x" * 101}, {"details.event"}),
        (SupportItem.JOB_CHANGE, {"activities": []}, {"details.activities", "details.separation"}),
        (
            SupportItem.JOB_CHANGE,
            {"activities": ["8.a"], "separation": "fired"},
            {"details.separation"},
        ),
    ],
)
def test_validate_details_per_kind(
    worker: Any, kind: SupportItem, details: dict[str, Any], keys: set[str]
) -> None:
    with pytest.raises(ValidationError) as error:
        support_records.validate(worker, transfer(worker, kind=kind, details=details), today=TODAY)
    assert error_keys(error) == keys


def test_details_are_saved_in_catalog_order_and_shown_with_labels(worker: Any) -> None:
    data = transfer(
        worker,
        kind=SupportItem.HOUSING_AND_CONTRACTS,
        details={"activities": ["3b.a", "3a.c"], "property_type": "owned", "extra": "x"},
    )
    record = support_records.create_record(worker, data, by=None, today=TODAY)
    assert record.details == {"activities": ["3a.c", "3b.a"], "property_type": "owned"}
    assert support_records.details_display(record) == [
        ("支援の内容", "社宅等の提供、口座開設"),
        ("物件の種類", "自己所有物件"),
    ]


def test_kinds_handled_elsewhere_are_rejected(worker: Any) -> None:
    with pytest.raises(ValidationError) as error:
        support_records.validate(
            worker, transfer(worker, kind=SupportItem.PERIODIC_INTERVIEW, details={}), today=TODAY
        )
    assert "kind" in error_keys(error)
    assert support_records.kind_or_none("periodic_interview") is None
    assert support_records.kind_or_none("job_change") is SupportItem.JOB_CHANGE


def test_deactivated_worker_cannot_get_records(worker: Any) -> None:
    workers.deactivate(worker, by=None, reason="重複")
    with pytest.raises(ValidationError) as error:
        support_records.create_record(worker, transfer(worker), by=None, today=TODAY)
    assert "__all__" in error_keys(error)


def test_body_language_follows_support_language() -> None:
    assert support_records.body_language_for(make_worker()) == BodyLanguage.ZH_HANS
    japanese = make_worker(support_language=SupportLanguage.objects.get(code="ja"))
    assert support_records.body_language_for(japanese) == BodyLanguage.JA


def test_plan_version_is_chosen_from_the_date(worker: Any) -> None:
    assert support_records.plan_version_for(worker, date(2026, 5, 1)) is None  # 計画なし
    host = worker.employment_contracts.get().host_organization
    plan = support_plans.create_plan(worker, host, applies_from=date(2026, 3, 20), by=None)
    first = support_plans.latest_version(plan)
    assert first is not None
    second = support_plans.add_version(
        plan,
        support_plans.content_of(first),
        applies_from=date(2026, 7, 1),
        change_reason="見直し",
        by=None,
    )
    # 入国時の送迎は契約の開始（4/1）前でも、その受入れ機関の計画を見る
    assert support_records.plan_version_for(worker, date(2026, 3, 31)) == first
    assert support_records.plan_version_for(worker, date(2026, 6, 30)) == first
    assert support_records.plan_version_for(worker, date(2026, 7, 1)) == second
    assert support_records.plan_version_for(worker, date(2026, 3, 1)) is None  # 適用開始前
    record = support_records.create_record(
        worker, transfer(worker, occurred_on=date(2026, 8, 1)), by=None, today=TODAY
    )
    assert record.plan_version == second


def test_plan_prefers_the_upcoming_contract_over_a_nearer_past_one() -> None:
    """実施日の直前に終わった前の契約より、これから始まる契約の計画を見る（転籍後の送迎など）。"""
    worker = make_worker(assigned_staff=make_staff(name="佐藤"))
    old_site = make_site(make_host(name="旧工業株式会社", corporate_number=VALID_NUMBERS[0]))
    new_site = make_site(make_host(name="新工業株式会社", corporate_number=VALID_NUMBERS[1]))
    old = make_contract(
        worker,
        site=old_site,
        changed_on=date(2025, 4, 1),
        starts_on=date(2025, 4, 1),
        ends_on=date(2026, 3, 31),
    )
    new = make_contract(
        worker,
        site=new_site,
        changed_on=date(2026, 6, 1),
        starts_on=date(2026, 6, 1),
        ends_on=date(2027, 5, 31),
    )
    old_plan = support_plans.create_plan(
        worker, old.host_organization, applies_from=date(2025, 3, 1), by=None
    )
    new_plan = support_plans.create_plan(
        worker, new.host_organization, applies_from=date(2026, 4, 1), by=None
    )
    # 4/2 は前の契約の終了（3/31）から 2 日、次の契約の開始（6/1）まで 60 日。それでも次の契約
    assert support_records.contract_on(worker, date(2026, 4, 2)) == new
    assert support_records.plan_version_for(worker, date(2026, 4, 2)) == (
        support_plans.latest_version(new_plan)
    )
    # 契約の期間内はその契約。次の契約が無い日（最後の契約の終了後）だけ、直近の過去の契約
    assert support_records.contract_on(worker, date(2025, 10, 1)) == old
    assert support_records.plan_version_for(worker, date(2025, 10, 1)) == (
        support_plans.latest_version(old_plan)
    )
    assert support_records.contract_on(worker, date(2027, 6, 30)) == new
    assert support_records.contract_on(worker, date(2025, 1, 1)) == old  # 最初の契約の前
    assert support_records.contract_on(make_worker(), date(2026, 4, 2)) is None


def test_void_record_requires_reason_and_keeps_the_row(worker: Any) -> None:
    record = support_records.create_record(worker, transfer(worker), by=None, today=TODAY)
    with pytest.raises(ValidationError):
        support_records.void_record(record, by=None, reason=" ")
    support_records.void_record(record, by=None, reason="日付の誤り")
    record.refresh_from_db()
    assert record.voided_at is not None and record.void_reason == "日付の誤り"
    with pytest.raises(ValidationError):
        support_records.void_record(record, by=None, reason="もう一度")
    with pytest.raises(ValidationError):
        support_records.add_attachments(record, [ContentFile(PDF_BYTES, name="a.pdf")], by=None)
    entry = AuditLog.objects.filter(
        action=AuditAction.UPDATE, content_type__model="supportrecord", object_id=str(record.pk)
    ).get()
    assert entry.changes["void_reason"] == ["", "日付の誤り"]


def test_attachment_void_keeps_the_file_and_is_audited(worker: Any) -> None:
    record = support_records.create_record(
        worker, transfer(worker), by=None, files=[ContentFile(PDF_BYTES, name="a.pdf")], today=TODAY
    )
    attachment = record.attachments.get()
    with pytest.raises(ValidationError):
        attachments.void_attachment(attachment, by=None, reason="")
    attachments.void_attachment(attachment, by=None, reason="別の人の書類")
    attachment.refresh_from_db()
    assert attachment.voided_at is not None and attachment.file.read() == PDF_BYTES
    assert not attachments.active(record.attachments.all()).exists()
    entry = AuditLog.objects.filter(
        action=AuditAction.UPDATE, content_type__model="attachment", object_id=str(attachment.pk)
    ).get()
    assert entry.changes["void_reason"] == ["", "別の人の書類"]
    with pytest.raises(ValidationError):
        attachments.void_attachment(attachment, by=None, reason="もう一度")


def test_voided_certificate_is_not_used_for_the_expiry_filing() -> None:
    """取り消した構成員証明書は、有効期限の起票に使わない（1 つ前の証明書を見る）。"""
    from tests.core.factories import make_organization

    org = make_organization()
    older = attachments.create_attachment(
        target=org,
        kind=AttachmentKind.FOOD_COUNCIL_CERTIFICATE,
        file=ContentFile(PDF_BYTES, name="old.pdf"),
        created_by=None,
        valid_until=date(2027, 3, 31),
    )
    newer = attachments.create_attachment(
        target=org,
        kind=AttachmentKind.FOOD_COUNCIL_CERTIFICATE,
        file=ContentFile(PDF_BYTES, name="wrong.pdf"),
        created_by=None,
        valid_until=date(2030, 3, 31),
    )
    attachments.void_attachment(newer, by=None, reason="期限の誤り")
    latest = filings._latest_attachments_with_expiry()
    assert [a.pk for a in latest if a.kind == AttachmentKind.FOOD_COUNCIL_CERTIFICATE] == [older.pk]


def test_counts_per_worker_and_kind(worker: Any) -> None:
    other = make_worker(
        assigned_staff=worker.assigned_staff
    )  # 契約が無くても記録できる（計画なし）
    exchange = {"event": "地域の夏祭り"}
    for day in (date(2026, 9, 1), date(2026, 9, 20), date(2026, 10, 2)):
        support_records.create_record(
            worker,
            transfer(worker, kind=SupportItem.EXCHANGE, occurred_on=day, details=exchange),
            by=None,
            today=TODAY,
        )
    voided = support_records.create_record(
        worker,
        transfer(
            worker, kind=SupportItem.EXCHANGE, occurred_on=date(2026, 9, 25), details=exchange
        ),
        by=None,
        today=TODAY,
    )
    support_records.void_record(voided, by=None, reason="重複")
    support_records.create_record(
        other, transfer(other, occurred_on=date(2026, 9, 10)), by=None, today=TODAY
    )
    result = support_records.counts([worker.pk, other.pk], date(2026, 9, 1), date(2026, 9, 30))
    assert result == {
        (worker.pk, "exchange"): support_records.KindSummary(2, date(2026, 9, 20)),
        (other.pk, "airport_transfer"): support_records.KindSummary(1, date(2026, 9, 10)),
    }


def test_choice_codes_are_sub_items_of_the_form() -> None:
    """選択肢の値は参考様式第1-17号の細目の符号で、種別の区分と一致する（ADR-0007）。"""
    from services.support_catalog import ACTIVITY_LABELS, FORM_SECTION, ROWS, section_prefix

    stored = []
    for kind, specs in support_records.DETAIL_FIELDS.items():
        for spec in specs:
            if spec.name in ("property_type", "separation"):  # 様式の細目ではない選択肢
                continue
            for code, _label in spec.choices:
                if code == "other":
                    continue
                assert code in ACTIVITY_LABELS, code
                assert FORM_SECTION[kind].startswith(section_prefix(code)[0]), (kind, code)
                prefix, letter = code.split(".")
                assert any(
                    section_prefix(row.code) == prefix and letter in row.letters for row in ROWS
                ), code
                stored.append(code)
    assert len(stored) == len(set(stored)) == 20  # ADR-0007 の対応表の行数（other を除く）
