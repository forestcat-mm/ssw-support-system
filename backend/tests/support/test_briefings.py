"""事前ガイダンス・生活オリエンテーション（T21、ADR-0009）: チェックリストの版・入力の検査・
所要時間と未チェックの警告・費用の確認・署名済みの確認書・取消・集計・画面。"""

import json
from dataclasses import replace
from datetime import date, time
from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import translation

from apps.core.models import AttachmentKind, AuditAction, AuditLog
from apps.support.models import (
    Briefing,
    BriefingCheck,
    BriefingSession,
    ImmutableRowError,
    SupportRecord,
)
from services import attachments, briefings, support_plans, support_records, workers
from services.briefings import BriefingInput, CheckInput, FeeInput, SessionInput
from services.support_catalog import ACTIVITY_LABELS, SupportItem
from tests.conftest import PDF_BYTES
from tests.contracts.factories import make_contract, site_with_categories
from tests.core.factories import make_staff
from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 17)
ORIENTATION = SupportItem.ORIENTATION
GUIDANCE = SupportItem.PRE_GUIDANCE


@pytest.fixture
def worker() -> Any:
    worker = make_worker(assigned_staff=make_staff(name="佐藤", position="支援担当"))
    make_contract(worker, site=site_with_categories())
    return worker


def session(day: int, start: int, end: int, month: int = 10) -> SessionInput:
    return SessionInput(date(2026, month, day), time(start), time(end))


def all_checked(kind: SupportItem, *, skip: tuple[str, ...] = ()) -> dict[str, CheckInput]:
    checklist = briefings.checklist_for(kind, TODAY)
    return {code: CheckInput("done") for code in checklist.item_codes if code not in skip}


def orientation(worker: Any, **overrides: Any) -> BriefingInput:
    values: dict[str, Any] = {
        "kind": ORIENTATION,
        "method": "in_person",
        "staff": worker.assigned_staff,
        "place": "受入れ機関の会議室",
        "sessions": [session(1, 9, 13), session(2, 9, 13)],
        "checks": all_checked(ORIENTATION),
    }
    return BriefingInput(**(values | overrides))


def guidance(worker: Any, **overrides: Any) -> BriefingInput:
    values: dict[str, Any] = {
        "kind": GUIDANCE,
        "method": "online",
        "identity_confirmed": True,
        "staff": worker.assigned_staff,
        "place": "オンライン（自機関の事務所）",
        "sessions": [session(1, 13, 16, month=9)],
        "checks": all_checked(GUIDANCE),
        "deposit_contract": "none",
        "foreign_fees": "paid",
        "fees": [
            FeeInput("○○人材派遣公司", date(2026, 3, 1), 200000, "紹介料 15 万円、研修費 5 万円")
        ],
        "fees_agreed": True,
    }
    return BriefingInput(**(values | overrides))


def create(worker: Any, data: BriefingInput, **kwargs: Any) -> Briefing:
    return briefings.create_briefing(worker, data, by=None, today=TODAY, **kwargs)


def pdf(name: str = "確認書.pdf") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, PDF_BYTES, content_type="application/pdf")


def keys(error: pytest.ExceptionInfo[ValidationError]) -> set[str]:
    return set(error.value.error_dict)


def warning_codes(briefing: Briefing) -> list[str]:
    return [warning.code for warning in briefings.warnings(briefing)]


# --- チェックリストの版（データ） -----------------------------------------------------------


def test_checklist_sections_match_the_support_plan_form() -> None:
    """欄の符号は第1-17号の細目（支援計画と同じ）。確認書の番号は 5-9 が 1〜9、5-8 が 1〜6。"""
    guidance_list = briefings.checklist_for(GUIDANCE, TODAY)
    orientation_list = briefings.checklist_for(ORIENTATION, TODAY)
    assert [s.code for s in guidance_list.sections] == [f"1.{c}" for c in "abcdefghij"]
    assert [s.form_number for s in guidance_list.sections] == [*range(1, 10), None]
    assert [s.code for s in orientation_list.sections] == [f"4.{c}" for c in "abcdef"]
    assert [s.form_number for s in orientation_list.sections] == list(range(1, 7))
    for checklist in (guidance_list, orientation_list):
        assert len(set(checklist.item_codes)) == len(checklist.item_codes)
        for section in checklist.sections:
            assert section.title == ACTIVITY_LABELS[section.code]
            assert section.items and all(
                item.code.startswith(section.code + ".") and item.label for item in section.items
            )
    assert len(guidance_list.items) == 12 and len(orientation_list.items) == 29


def test_checklist_versions_keep_sources_and_rules() -> None:
    data = json.loads(briefings.DATA_FILE.read_text(encoding="utf-8"))
    version = data["versions"][0]
    assert version["id"] == version["effective_from"] == "2025-04-01"
    for source in version["sources"]:
        assert source["url"].startswith("https://www.moj.go.jp/isa/content/")
        assert source["retrieved_on"] == "2026-10-17" and len(source["sha256"]) == 64
    guidance_rule = briefings.checklist_for(GUIDANCE, TODAY).duration
    orientation_rule = briefings.checklist_for(ORIENTATION, TODAY).duration
    assert (guidance_rule.guideline_minutes, guidance_rule.minimum_minutes) == (180, 60)
    assert (orientation_rule.guideline_minutes, orientation_rule.minimum_minutes) == (480, 240)
    assert (guidance_rule.guideline_kind, orientation_rule.guideline_kind) == ("about", "at_least")


def test_checklist_version_is_chosen_from_the_date(tmp_path: Any) -> None:
    data = json.loads(briefings.DATA_FILE.read_text(encoding="utf-8"))
    newer = json.loads(json.dumps(data["versions"][0]))
    newer["id"] = newer["effective_from"] = "2027-04-01"
    newer["kinds"]["orientation"]["sections"][0]["items"].append(
        {"code": "4.a.10", "label": "改正で足した細目"}
    )
    data["versions"].append(newer)
    path = tmp_path / "checklists.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    versions = briefings._load(path)
    assert [version.id for version in versions] == ["2025-04-01", "2027-04-01"]
    original = briefings.VERSIONS
    briefings.VERSIONS = versions
    try:
        assert briefings.checklist_for(ORIENTATION, date(2027, 3, 31)).version == "2025-04-01"
        later = briefings.checklist_for(ORIENTATION, date(2027, 4, 1))
        assert later.version == "2027-04-01" and "4.a.10" in later.item_codes
        # 最も古い版の適用開始より前の日は、最も古い版で読む
        assert briefings.checklist_for(ORIENTATION, date(2020, 1, 1)).version == "2025-04-01"
    finally:
        briefings.VERSIONS = original


# --- 記録 -------------------------------------------------------------------------------


def test_create_orientation_copies_dates_and_total_to_the_record(worker: Any) -> None:
    data = orientation(
        worker,
        sessions=[session(2, 13, 17), session(1, 9, 12)],  # 入力の順は問わない
        checks=all_checked(ORIENTATION, skip=("4.a.9",))
        | {"4.a.9": CheckInput("not_applicable", " 対象外（本人の希望） ")},
        legal_info_provider="○○法律事務所 山田",
        interpreter="通訳 王",
        body="質問に回答した。",
    )
    briefing = create(worker, data)
    record = briefing.record
    assert (record.kind, record.counterpart, record.method) == (
        "orientation",
        "worker",
        "in_person",
    )
    assert (record.occurred_on, record.start_time, record.duration_minutes) == (
        date(2026, 10, 1),
        time(9),
        7 * 60,
    )
    assert [(s.occurred_on.day, s.minutes) for s in briefing.sessions.all()] == [(1, 180), (2, 240)]
    assert briefing.checklist_version == "2025-04-01"
    assert briefing.legal_info_provider == "○○法律事務所 山田"
    not_applicable = briefing.checks.get(code="4.a.9")
    assert (not_applicable.status, not_applicable.reason) == (
        "not_applicable",
        "対象外（本人の希望）",
    )
    assert briefings.check_counts(briefing) == briefings.CheckCounts(28, 1, 0)
    states = briefings.checklist_states(briefing)
    assert all(state.complete for state in states)  # 該当なしも欄の完了に数える
    assert (
        AuditLog.objects.filter(
            action=AuditAction.CREATE, content_type__model="briefingcheck"
        ).count()
        == 29
    )


def test_section_is_complete_only_when_all_items_are_selected(worker: Any) -> None:
    briefing = create(worker, orientation(worker, checks=all_checked(ORIENTATION, skip=("4.e.4",))))
    states = {state.section.code: state for state in briefings.checklist_states(briefing)}
    assert not states["4.e"].complete and states["4.a"].complete
    assert briefings.check_counts(briefing) == briefings.CheckCounts(28, 0, 1)
    assert "missing_items" in warning_codes(briefing)


def test_validate_sessions(worker: Any) -> None:
    with pytest.raises(ValidationError) as error:
        create(worker, orientation(worker, sessions=[]))
    assert keys(error) == {"sessions"}
    with pytest.raises(ValidationError) as error:
        create(
            worker,
            orientation(
                worker,
                sessions=[session(1, 9, 13), session(1, 12, 15), session(2, 10, 10)],
            ),
        )
    assert keys(error) == {"sessions.1.start_time", "sessions.2.end_time"}
    with pytest.raises(ValidationError) as error:
        create(worker, orientation(worker, sessions=[session(18, 9, 13)]))  # 未来の日
    assert keys(error) == {"sessions.0.occurred_on"}
    # 前の行の終了と同じ時刻に始まる行は重ならない
    create(worker, orientation(worker, sessions=[session(1, 9, 12), session(1, 12, 15)]))


def test_validate_checks(worker: Any) -> None:
    checks = all_checked(ORIENTATION) | {
        "4.a.1": CheckInput("not_applicable", "  "),
        "4.a.2": CheckInput("maybe"),
        "9.z.1": CheckInput("done"),
    }
    with pytest.raises(ValidationError) as error:
        create(worker, orientation(worker, checks=checks))
    assert keys(error) == {"checks.4.a.1", "checks.4.a.2", "checks"}
    assert not SupportRecord.objects.exists()


def test_methods_and_identity_check(worker: Any) -> None:
    with pytest.raises(ValidationError) as error:
        create(worker, guidance(worker, identity_confirmed=False))
    assert keys(error) == {"identity_confirmed"}
    for method in ("video", "phone"):
        with pytest.raises(ValidationError) as error:
            create(worker, guidance(worker, method=method))
        assert keys(error) == {"method"}
    with pytest.raises(ValidationError) as error:
        create(worker, orientation(worker, method="phone"))
    assert keys(error) == {"method"}
    video = create(worker, orientation(worker, method="video"))
    assert video.record.method == "video"
    in_person = create(worker, guidance(worker, method="in_person", identity_confirmed=True))
    assert in_person.identity_confirmed is False  # 本人確認の印はオンラインのときだけ持つ


def test_place_staff_and_deactivated_worker(worker: Any) -> None:
    with pytest.raises(ValidationError) as error:
        create(worker, orientation(worker, place=" ", staff=None))
    assert keys(error) == {"place", "staff"}
    workers.deactivate(worker, by=None, reason="重複")
    with pytest.raises(ValidationError) as error:
        create(worker, orientation(worker))
    assert "__all__" in keys(error)


def test_rows_cannot_be_changed(worker: Any) -> None:
    briefing = create(worker, orientation(worker))
    check = briefing.checks.first()
    assert check is not None
    with pytest.raises(ImmutableRowError):
        check.save()
    first_session = briefing.sessions.first()
    assert isinstance(first_session, BriefingSession)
    with pytest.raises(ImmutableRowError):
        first_session.delete()


# --- 保証金・外国の機関に支払った費用（事前ガイダンス） -------------------------------------------


def test_guidance_saves_deposit_and_fees(worker: Any) -> None:
    briefing = create(worker, guidance(worker))
    assert (briefing.deposit_contract, briefing.foreign_fees, briefing.fees_agreed) == (
        "none",
        "paid",
        True,
    )
    fee = briefing.fees.get()
    assert (fee.organization_name, fee.paid_on, fee.amount_jpy) == (
        "○○人材派遣公司",
        date(2026, 3, 1),
        200000,
    )
    assert briefing.identity_confirmed is True
    assert AuditLog.objects.filter(
        action=AuditAction.CREATE, content_type__model="briefingforeignfee"
    ).exists()


def test_validate_deposit_and_fees(worker: Any) -> None:
    with pytest.raises(ValidationError) as error:
        create(worker, guidance(worker, deposit_contract="", foreign_fees="", fees=[]))
    assert keys(error) == {"deposit_contract", "foreign_fees"}
    with pytest.raises(ValidationError) as error:
        create(worker, guidance(worker, deposit_contract="exists", fees=[], fees_agreed=None))
    assert keys(error) == {"deposit_collector", "deposit_detail", "fees", "fees_agreed"}
    with pytest.raises(ValidationError) as error:
        create(worker, guidance(worker, foreign_fees="none"))  # 支払が無いのに行と合意がある
    assert keys(error) == {"fees", "fees_agreed"}
    with pytest.raises(ValidationError) as error:
        create(worker, guidance(worker, deposit_collector="ブローカー"))
    assert keys(error) == {"deposit_collector"}
    with pytest.raises(ValidationError) as error:
        create(worker, guidance(worker, fees=[FeeInput(" ", None, 0, "")]))
    assert keys(error) == {
        "fees.0.organization_name",
        "fees.0.paid_on",
        "fees.0.amount_jpy",
        "fees.0.breakdown",
    }
    none_paid = create(worker, guidance(worker, foreign_fees="none", fees=[], fees_agreed=None))
    assert (none_paid.foreign_fees, none_paid.fees.count()) == ("none", 0)


def test_deposit_contract_and_disagreement_are_strong_warnings(worker: Any) -> None:
    briefing = create(
        worker,
        guidance(
            worker,
            deposit_contract="exists",
            deposit_collector="送出機関 A",
            deposit_detail="保証金 10 万円",
            fees_agreed=False,
        ),
    )
    strong = {warning.code for warning in briefings.warnings(briefing) if warning.strong}
    assert strong == {"deposit_contract", "fees_not_agreed"}


def test_orientation_ignores_guidance_only_fields(worker: Any) -> None:
    briefing = create(
        worker,
        orientation(
            worker,
            deposit_contract="exists",
            foreign_fees="paid",
            fees=[FeeInput("A", date(2026, 3, 1), 1, "x")],
            fees_agreed=False,
            identity_confirmed=True,
        ),
    )
    assert (briefing.deposit_contract, briefing.foreign_fees, briefing.fees_agreed) == (
        "",
        "",
        None,
    )
    assert briefing.fees.count() == 0 and briefing.identity_confirmed is False


# --- 所要時間・行数・計画の警告 ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "minutes", "continuing", "expected"),
    [
        (GUIDANCE, 59, True, ["below_minimum"]),
        (GUIDANCE, 60, True, []),
        (GUIDANCE, 60, False, ["below_guideline"]),
        (GUIDANCE, 179, False, ["below_guideline"]),
        (GUIDANCE, 180, False, []),
        (ORIENTATION, 239, True, ["below_minimum"]),
        (ORIENTATION, 240, True, []),
        (ORIENTATION, 479, False, ["below_guideline"]),
        (ORIENTATION, 480, False, []),
    ],
)
def test_duration_warnings_boundaries(
    kind: SupportItem, minutes: int, continuing: bool, expected: list[str]
) -> None:
    rule = briefings.checklist_for(kind, TODAY).duration
    result = briefings.duration_warnings(kind, minutes, rule, continuing=continuing)
    assert [warning.code for warning in result] == expected


def test_duration_messages_tell_guideline_from_minimum() -> None:
    guidance_rule = briefings.checklist_for(GUIDANCE, TODAY).duration
    orientation_rule = briefings.checklist_for(ORIENTATION, TODAY).duration
    with translation.override("ja"):
        messages = [
            briefings.duration_warnings(GUIDANCE, 120, guidance_rule, continuing=False)[0].message,
            briefings.duration_warnings(ORIENTATION, 300, orientation_rule, continuing=False)[
                0
            ].message,
            briefings.duration_warnings(GUIDANCE, 30, guidance_rule, continuing=True)[0].message,
            briefings.duration_warnings(ORIENTATION, 200, orientation_rule, continuing=True)[
                0
            ].message,
        ]
    assert messages == [
        "目安の 3 時間を下回っています。",
        "必要な 8 時間以上を満たしていません。",
        "下限の 1 時間に満たないため、事前ガイダンスを適切に行ったとはいえません。",
        "下限の 4 時間に満たないため、生活オリエンテーションを適切に行ったとはいえません。",
    ]


def test_more_than_three_rows_do_not_fit_the_form(worker: Any) -> None:
    three = create(worker, orientation(worker, sessions=[session(d, 9, 12) for d in (1, 2, 3)]))
    four = create(worker, orientation(worker, sessions=[session(d, 9, 11) for d in (5, 6, 7, 8)]))
    assert "too_many_rows" not in warning_codes(three)
    assert "too_many_rows" in warning_codes(four)
    with translation.override("ja"):
        message = next(w.message for w in briefings.warnings(four) if w.code == "too_many_rows")
    assert "確認書の欄に収まりません" in message


def test_shorter_than_the_planned_hours(worker: Any) -> None:
    host = worker.employment_contracts.get().host_organization
    content = support_plans.content_for_worker(worker)
    content.items[ORIENTATION] = replace(content.items[ORIENTATION], planned_hours=Decimal("10"))
    support_plans.create_plan(worker, host, applies_from=date(2026, 4, 1), by=None, content=content)
    briefing = create(worker, orientation(worker))  # 8 時間
    assert briefing.record.plan_version is not None
    assert briefings.planned_minutes(briefing) == 600
    assert "below_plan" in warning_codes(briefing)
    assert "below_guideline" not in warning_codes(briefing)


# --- 署名済みの確認書 ----------------------------------------------------------------------


def test_confirmation_needs_the_date_and_the_file(worker: Any) -> None:
    with pytest.raises(ValidationError) as error:
        create(worker, orientation(worker), confirmations=[pdf()])
    assert keys(error) == {"signed_on"}
    with pytest.raises(ValidationError) as error:
        create(worker, orientation(worker, signed_on=date(2026, 10, 2)))
    assert keys(error) == {"confirmation"}
    with pytest.raises(ValidationError) as error:
        create(worker, orientation(worker, signed_on=date(2026, 10, 1)), confirmations=[pdf()])
    assert keys(error) == {"signed_on"}  # 最後の実施日より前
    briefing = create(
        worker, orientation(worker, signed_on=date(2026, 10, 2)), confirmations=[pdf()]
    )
    attachment = briefing.record.attachments.get()
    assert attachment.kind == AttachmentKind.ORIENTATION_CONFIRMATION
    assert "unsigned" not in warning_codes(briefing)


def test_add_confirmation_later(worker: Any) -> None:
    briefing = create(worker, guidance(worker))
    assert "unsigned" in warning_codes(briefing)
    with pytest.raises(ValidationError) as error:
        briefings.add_confirmation(briefing, [], signed_on=None, by=None, today=TODAY)
    assert keys(error) == {"confirmation", "signed_on"}
    briefings.add_confirmation(briefing, [pdf()], signed_on=date(2026, 9, 2), by=None, today=TODAY)
    briefing.refresh_from_db()
    assert briefing.signed_on == date(2026, 9, 2)
    assert briefing.record.attachments.get().kind == AttachmentKind.PRE_GUIDANCE_CONFIRMATION
    assert "unsigned" not in warning_codes(briefing)
    assert AuditLog.objects.filter(
        action=AuditAction.UPDATE, content_type__model="briefing", object_id=str(briefing.pk)
    ).exists()


def test_signed_needs_an_active_confirmation_and_the_date(worker: Any) -> None:
    """署名済み = 取り消していない確認書の添付があり、署名日が入っている（一覧と詳細で同じ）。"""
    briefing = create(
        worker, orientation(worker, signed_on=date(2026, 10, 2)), confirmations=[pdf()]
    )
    assert briefings.is_signed(briefing)
    other = briefing.record.attachments.get()
    attachments.void_attachment(other, by=None, reason="別の人の確認書")
    briefing.refresh_from_db()
    assert briefing.signed_on == date(2026, 10, 2)  # 署名日だけが残っている
    assert not briefings.is_signed(briefing)
    assert "unsigned" in warning_codes(briefing)
    # 確認書以外の添付（写真など）では署名済みにならない
    support_records.add_attachments(briefing.record, [pdf("写真.pdf")], by=None)
    assert not briefings.is_signed(briefing)
    # 署名日が無ければ、有効な確認書があっても署名済みにしない
    unsigned = create(worker, orientation(worker, sessions=[session(5, 9, 17)]))
    attachments.create_attachment(
        target=unsigned.record,
        kind=AttachmentKind.ORIENTATION_CONFIRMATION,
        file=pdf(),
        created_by=None,
    )
    assert unsigned.signed_on is None and not briefings.is_signed(unsigned)


# --- 取消・集計 ------------------------------------------------------------------------


def test_void_keeps_rows_and_is_excluded_from_summary(worker: Any) -> None:
    kept = create(
        worker,
        orientation(
            worker,
            checks=all_checked(ORIENTATION, skip=("4.a.1", "4.a.2"))
            | {"4.a.1": CheckInput("not_applicable", "前の受入れ機関で説明済み")},
        ),
    )
    voided = create(worker, orientation(worker, sessions=[session(5, 9, 17)]))
    with pytest.raises(ValidationError):
        support_records.void_record(voided.record, by=None, reason="誤り")  # 画面ごとに取り消す
    with pytest.raises(ValidationError):
        briefings.void_briefing(voided, by=None, reason=" ")
    briefings.void_briefing(voided, by=None, reason="別の人の記録")
    voided.record.refresh_from_db()
    assert voided.record.voided_at is not None and voided.record.void_reason == "別の人の記録"
    assert BriefingCheck.objects.filter(briefing=voided).count() == 29  # 行は残る
    assert briefings.warnings(voided) == []
    guidance_record = create(worker, guidance(worker))
    result = briefings.summary([worker.pk], date(2026, 9, 1), date(2026, 10, 31))
    assert result[(worker.pk, "orientation")] == briefings.BriefingSummary(
        count=1, last_on=date(2026, 10, 1), minutes=480, done=27, not_applicable=1, missing=1
    )
    assert result[(worker.pk, "pre_guidance")].count == 1
    assert kept.pk and guidance_record.pk
    # 支援実績の件数（T24）でも、取り消した記録は数えない
    counts = support_records.counts([worker.pk], date(2026, 9, 1), date(2026, 10, 31))
    assert counts[(worker.pk, "orientation")].count == 1


# --- 画面 -------------------------------------------------------------------------------


def records_url(worker: Any) -> str:
    return f"/workers/{worker.pk}/support/records/"


def form_url(worker: Any, kind: str) -> str:
    return f"/workers/{worker.pk}/support/briefings/new/{kind}/"


def management(prefix: str, total: int, initial: int = 0) -> dict[str, Any]:
    return {
        f"{prefix}-TOTAL_FORMS": total,
        f"{prefix}-INITIAL_FORMS": initial,
        f"{prefix}-MIN_NUM_FORMS": 0,
        f"{prefix}-MAX_NUM_FORMS": 1000,
    }


def check_fields(kind: SupportItem, *, skip: tuple[str, ...] = ()) -> dict[str, str]:
    checklist = briefings.checklist_for(kind, TODAY)
    return {
        "check_" + code.replace(".", "_"): "done"
        for code in checklist.item_codes
        if code not in skip
    }


def post_orientation(client: Client, worker: Any, **overrides: Any) -> Any:
    url = form_url(worker, "orientation")
    page = client.get(url)
    assert page.status_code == 200
    data: dict[str, Any] = {
        "once_token": page.context["once_token"],
        "method": "in_person",
        "staff": worker.assigned_staff_id,
        "place": "受入れ機関の会議室",
        "body_language": "ja",
        **management("sessions", 3, 1),
        "sessions-0-occurred_on": "2026-10-01",
        "sessions-0-start_time": "09:00",
        "sessions-0-end_time": "12:00",
        "sessions-1-occurred_on": "2026-10-02",
        "sessions-1-start_time": "09:00",
        "sessions-1-end_time": "12:00",
        # 3 行目は足したが空のまま（無視する）
        **check_fields(ORIENTATION, skip=("4.a.9", "4.e.4")),
        "check_4_a_9": "not_applicable",
        "reason_4_a_9": "本人に子どもがいない",
    }
    data.update(overrides)
    return client.post(url, data)


def test_form_shows_checklist_rows_and_defaults(support_staff_client: Client, worker: Any) -> None:
    page = support_staff_client.get(form_url(worker, "orientation"))
    editor = page.context["editor"]
    assert editor.form["staff"].value() == worker.assigned_staff_id
    html = page.content.decode()
    assert 'data-formset="sessions"' in html and "__prefix__" in html
    assert 'name="check_4_a_1"' in html and 'name="reason_4_a_1"' in html
    assert 'name="legal_info_provider"' in html and 'name="deposit_contract"' not in html
    assert '"video"' in html  # 動画視聴は生活オリエンテーションだけ
    guidance_page = support_staff_client.get(form_url(worker, "pre_guidance")).content.decode()
    assert 'name="deposit_contract"' in guidance_page and 'data-formset="fees"' in guidance_page
    assert '"video"' not in guidance_page and 'name="legal_info_provider"' not in guidance_page
    assert support_staff_client.get(form_url(worker, "exchange")).status_code == 404


def test_create_orientation_on_screen_shows_warnings(
    support_staff_client: Client, worker: Any
) -> None:
    response = post_orientation(support_staff_client, worker)
    briefing = Briefing.objects.get(worker=worker)
    assert response["Location"] == f"/workers/{worker.pk}/support/briefings/{briefing.pk}/"
    assert briefing.sessions.count() == 2 and briefing.record.duration_minutes == 360
    assert briefing.checks.get(code="4.a.9").reason == "本人に子どもがいない"
    html = support_staff_client.get(response["Location"]).content.decode()
    assert "必要な 8 時間以上を満たしていません。" in html
    assert "未チェックの必須項目が 1 件あります。" in html
    assert "署名済みの確認書が添付されていません。" in html
    assert 'data-done="27" data-not-applicable="1" data-missing="1"' in html
    assert 'data-item="4.e.4" data-status=""' in html
    entry = AuditLog.objects.filter(action=AuditAction.VIEW, object_id=str(worker.pk)).latest("pk")
    assert entry.context["shown"]["briefing"] == briefing.pk

    listing = support_staff_client.get(records_url(worker)).content.decode()
    assert f"/support/briefings/{briefing.pk}/" in listing and "未署名" in listing
    # 支援実績の行の取消（T18 の画面）は使わず、この記録の画面で取り消す
    t18_void = f"{records_url(worker)}{briefing.record_id}/void/"
    assert support_staff_client.get(t18_void).status_code == 404


def test_errors_go_back_to_rows_and_items(support_staff_client: Client, worker: Any) -> None:
    response = post_orientation(
        support_staff_client, worker, **{"sessions-1-end_time": "08:00", "reason_4_a_9": ""}
    )
    assert response.status_code == 200
    html = response.content.decode()
    assert "終了の時刻は開始より後にしてください。" in html  # 行の欄（サービスの検査）
    assert "該当なしの理由を入力してください。" in html  # 細目の理由（サービスの検査）
    # 入力の形の誤り（フォームの検査）は、サービスの検査より先に出る
    empty_place = post_orientation(support_staff_client, worker, place="")
    assert 'id="id_place-error"' in empty_place.content.decode()
    assert not Briefing.objects.exists()


def test_create_guidance_with_fees_on_screen(support_staff_client: Client, worker: Any) -> None:
    url = form_url(worker, "pre_guidance")
    page = support_staff_client.get(url)
    data: dict[str, Any] = {
        "once_token": page.context["once_token"],
        "method": "online",
        "identity_confirmed": "on",
        "staff": worker.assigned_staff_id,
        "place": "オンライン",
        "body_language": "ja",
        **management("sessions", 1, 1),
        "sessions-0-occurred_on": "2026-09-01",
        "sessions-0-start_time": "13:00",
        "sessions-0-end_time": "16:00",
        **check_fields(GUIDANCE),
        "deposit_contract": "none",
        "foreign_fees": "paid",
        "fees_agreed": "yes",
        **management("fees", 2),
        "fees-0-organization_name": "○○人材派遣公司",
        "fees-0-paid_on": "2026-03-01",
        "fees-0-amount_jpy": "200000",
        "fees-0-breakdown": "紹介料",
        "signed_on": "2026-09-01",
        "confirmation": [
            SimpleUploadedFile("確認書.pdf", PDF_BYTES, content_type="application/pdf")
        ],
    }
    response = support_staff_client.post(url, data)
    briefing = Briefing.objects.get(worker=worker)
    assert response.status_code == 302
    assert briefing.fees.get().amount_jpy == 200000 and briefing.fees_agreed is True
    assert briefing.signed_on == date(2026, 9, 1)
    html = support_staff_client.get(response["Location"]).content.decode()
    assert 'id="briefing-warnings"' not in html  # 3 時間・全項目・署名済み
    assert "○○人材派遣公司" in html and "確認書.pdf" in html


def test_attach_confirmation_and_void_on_screen(support_staff_client: Client, worker: Any) -> None:
    briefing = create(worker, orientation(worker))
    base = f"/workers/{worker.pk}/support/briefings/{briefing.pk}/"
    page = support_staff_client.get(base + "confirmation/")
    missing_date = support_staff_client.post(
        base + "confirmation/",
        {"once_token": page.context["once_token"], "confirmation": [pdf()]},
    )
    assert 'id="id_signed_on-error"' in missing_date.content.decode()
    page = support_staff_client.get(base + "confirmation/")
    response = support_staff_client.post(
        base + "confirmation/",
        {
            "once_token": page.context["once_token"],
            "signed_on": "2026-10-03",
            "confirmation": [pdf()],
        },
    )
    assert response["Location"] == base
    assert (
        "署名済みの確認書が添付されていません。"
        not in support_staff_client.get(base).content.decode()
    )
    assert "署名済み" in support_staff_client.get(records_url(worker)).content.decode()

    confirm = support_staff_client.get(base + "void/")
    response = support_staff_client.post(
        base + "void/", {"once_token": confirm.context["once_token"], "reason": "別の人の記録"}
    )
    assert response["Location"] == base
    html = support_staff_client.get(base).content.decode()
    assert 'id="record-voided"' in html and 'id="briefing-warnings"' not in html
    assert support_staff_client.get(base + "void/").status_code == 404
    assert support_staff_client.get(base + "confirmation/").status_code == 404


def test_list_and_detail_agree_on_signed(support_staff_client: Client, worker: Any) -> None:
    """署名日があっても、有効な確認書の添付が無ければ一覧は「未署名」で、詳細にも警告が出る。"""
    briefing = create(
        worker, orientation(worker, signed_on=date(2026, 10, 2)), confirmations=[pdf()]
    )
    row = f'data-record="{briefing.record_id}"'

    def list_cell() -> str:
        html = support_staff_client.get(records_url(worker)).content.decode()
        start = html.index(row)
        return html[start : html.index("</tr>", start)]

    base = f"/workers/{worker.pk}/support/briefings/{briefing.pk}/"
    assert "（署名済み）" in list_cell()
    assert (
        "署名済みの確認書が添付されていません。"
        not in support_staff_client.get(base).content.decode()
    )
    attachments.void_attachment(briefing.record.attachments.get(), by=None, reason="別の人の確認書")
    assert "（未署名）" in list_cell()
    assert (
        "署名済みの確認書が添付されていません。" in support_staff_client.get(base).content.decode()
    )


def test_double_submit_creates_one_briefing(support_staff_client: Client, worker: Any) -> None:
    url = form_url(worker, "orientation")
    page = support_staff_client.get(url)
    data: dict[str, Any] = {
        "once_token": page.context["once_token"],
        "method": "in_person",
        "staff": worker.assigned_staff_id,
        "place": "会議室",
        "body_language": "ja",
        **management("sessions", 1, 1),
        "sessions-0-occurred_on": "2026-10-01",
        "sessions-0-start_time": "09:00",
        "sessions-0-end_time": "17:00",
    }
    support_staff_client.post(url, data)
    support_staff_client.post(url, data)
    assert Briefing.objects.count() == 1


def test_deactivated_worker_briefings_are_read_only(admin_client: Client, worker: Any) -> None:
    briefing = create(worker, orientation(worker))
    workers.deactivate(worker, by=None, reason="重複")
    base = f"/workers/{worker.pk}/support/briefings/{briefing.pk}/"
    html = admin_client.get(base).content.decode()
    assert "/void/" not in html and "/confirmation/" not in html
    assert admin_client.get(form_url(worker, "orientation")).status_code == 403
    assert admin_client.get(base + "void/").status_code == 403
