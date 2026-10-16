"""外国人台帳の admin（登録・在留期限の更新・取り消し・一意性・検索・権限・監査ログ）。"""

import json
from datetime import date
from typing import Any

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import Client

from apps.core.models import AuditAction, AuditLog, Country, SupportLanguage, User
from apps.workers.models import ForeignWorker, ResidenceRecord
from services import audit
from tests.core.factories import make_user, verified_client

from .factories import add_residence, make_worker

pytestmark = pytest.mark.django_db

URL = "/admin/workers/foreignworker/"
INLINES = ("residence_records", "passport_records", "addresses", "contacts")


def _form(
    worker: ForeignWorker | None = None, **inline_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """外国人の変更フォームの POST データ。既存の履歴行は id だけ送る（他の列は変更不可）。"""
    data: dict[str, Any] = {
        "name_romaji": worker.name_romaji if worker else "WANG FANG",
        "name_kanji": worker.name_kanji if worker else "王芳",
        "name_native": "",
        "nationality": Country.objects.get(code="CN").pk,
        "birth_date": "1998-05-01",
        "sex": "female",
        "support_language": SupportLanguage.objects.get(code="zh-hans").pk,
        "status": "residing",
        "assigned_staff": "",
    }
    for prefix in INLINES:
        existing = list(getattr(worker, prefix).order_by("pk")) if worker else []
        rows = [{"id": record.pk} for record in existing] + inline_rows.get(prefix, [])
        data |= {
            f"{prefix}-TOTAL_FORMS": str(len(rows)),
            f"{prefix}-INITIAL_FORMS": str(len(existing)),
            f"{prefix}-MIN_NUM_FORMS": "0",
            f"{prefix}-MAX_NUM_FORMS": "1000",
        }
        for i, row in enumerate(rows):
            data |= {f"{prefix}-{i}-{key}": value for key, value in row.items()}
    return data


def _residence_row(**overrides: Any) -> dict[str, Any]:
    return {
        "application_type": "change",
        "status_of_residence": "ssw_1",
        "card_number": "ab12345678cd",
        "permitted_on": "2025-04-01",
        "expires_on": "2026-04-01",
    } | overrides


def _errors(response: Any) -> str:
    return str(response.content.decode())


def test_register_worker_with_residence_from_admin(admin_client: Client, admin_user: User) -> None:
    response = admin_client.post(f"{URL}add/", _form(residence_records=[_residence_row()]))
    assert response.status_code == 302, _errors(response)[:3000]
    worker = ForeignWorker.objects.get()
    record = worker.residence_records.get()
    assert record.card_number == "AB12345678CD"  # 正規化して保存
    assert worker.confirmed_by == admin_user and worker.confirmed_at is not None
    assert record.created_by == admin_user


def test_viewing_worker_detail_is_audited(admin_client: Client, admin_user: User) -> None:
    """T05 の本来の完了条件: 外国人詳細の閲覧がログに残る。"""
    worker = make_worker()
    add_residence(worker)
    assert admin_client.get(f"{URL}{worker.pk}/change/").status_code == 200
    log = AuditLog.objects.get(action=AuditAction.VIEW)
    assert log.actor == admin_user
    assert log.content_type == ContentType.objects.get_for_model(ForeignWorker)
    assert log.object_id == str(worker.pk)
    assert log.ip_address == "127.0.0.1"


def test_renewal_adds_a_row_and_list_shows_latest_expiry(admin_client: Client) -> None:
    """T07 の完了条件: 在留期限を更新すると新行が増え、一覧に最新が出る。"""
    worker = make_worker()
    add_residence(worker, expires_on=date(2026, 4, 1))
    renewal = _residence_row(
        application_type="extension", permitted_on="2026-03-15", expires_on="2027-04-01"
    )
    response = admin_client.post(
        f"{URL}{worker.pk}/change/", _form(worker, residence_records=[renewal])
    )
    assert response.status_code == 302, _errors(response)[:3000]

    assert worker.residence_records.count() == 2
    changelist = admin_client.get(URL)
    [row] = changelist.context["cl"].result_list
    assert row.latest_residence.record.expires_on == date(2027, 4, 1)
    assert "2027年4月1日" in changelist.content.decode()


def test_existing_history_rows_cannot_be_changed(admin_client: Client) -> None:
    worker = make_worker()
    record = add_residence(worker, expires_on=date(2026, 4, 1))
    data = _form(worker)
    data["residence_records-0-expires_on"] = "2099-01-01"  # 無効化された欄なので無視される
    admin_client.post(f"{URL}{worker.pk}/change/", data)
    record.refresh_from_db()
    assert record.expires_on == date(2026, 4, 1)


def test_void_requires_reason_and_removes_row_from_latest(
    admin_client: Client, admin_user: User
) -> None:
    worker = make_worker()
    first = add_residence(worker, permitted_on=date(2024, 4, 1))
    wrong = add_residence(worker, permitted_on=date(2025, 4, 1), card_number="ZZ99999999ZZ")
    data = _form(worker)
    index = 1  # order_by("pk") で 2 番目が wrong
    data[f"residence_records-{index}-void"] = "on"
    assert admin_client.post(f"{URL}{worker.pk}/change/", data).status_code == 200  # 理由なし

    data[f"residence_records-{index}-void_reason_input"] = "番号の入力ミス"
    assert admin_client.post(f"{URL}{worker.pk}/change/", data).status_code == 302
    wrong.refresh_from_db()
    assert wrong.voided_by == admin_user and wrong.void_reason == "番号の入力ミス"
    assert ForeignWorker.objects.get(pk=worker.pk).latest_residence.record == first


def test_card_number_of_another_worker_is_rejected_with_message(admin_client: Client) -> None:
    owner = make_worker()
    add_residence(owner)
    other = make_worker(name_romaji="LI NA", name_kanji="李娜")
    data = _form(other, residence_records=[_residence_row(card_number="AB 1234 5678 CD")])
    response = admin_client.post(f"{URL}{other.pk}/change/", data)
    assert response.status_code == 200
    assert f"別の外国人（ID: {owner.pk}）に登録されています" in _errors(response)
    assert not other.residence_records.exists()


def test_invalid_card_format_is_rejected(admin_client: Client) -> None:
    worker = make_worker()
    data = _form(worker, residence_records=[_residence_row(card_number="A1234567")])
    response = admin_client.post(f"{URL}{worker.pk}/change/", data)
    assert response.status_code == 200
    assert "英字 2 桁・数字 8 桁・英字 2 桁" in _errors(response)


def test_expiry_must_be_after_permit(admin_client: Client) -> None:
    worker = make_worker()
    row = _residence_row(permitted_on="2026-04-01", expires_on="2026-03-01")
    response = admin_client.post(
        f"{URL}{worker.pk}/change/", _form(worker, residence_records=[row])
    )
    assert response.status_code == 200
    assert not worker.residence_records.exists()


def test_search_by_card_number_with_notation_variants(admin_client: Client) -> None:
    target = make_worker()
    add_residence(target)
    make_worker(name_romaji="LI NA", name_kanji="李娜")
    for query in ("AB12345678CD", "ab 1234 5678 cd", "ＡＢ１２３４５６７８ＣＤ"):
        result = admin_client.get(URL, {"q": query}).context["cl"].result_list
        assert [w.pk for w in result] == [target.pk]
    assert len(admin_client.get(URL, {"q": "李"}).context["cl"].result_list) == 1


def test_filter_by_latest_expiry_period(admin_client: Client) -> None:
    soon = make_worker()
    add_residence(soon, expires_on=date(2026, 11, 30))
    later = make_worker(name_romaji="LI NA")
    add_residence(later, card_number="XY00000000XY", expires_on=date(2028, 1, 31))
    result = (
        admin_client.get(
            URL,
            {
                "latest_residence__record__expires_on__gte": "2026-11-01",
                "latest_residence__record__expires_on__lte": "2026-12-31",
            },
        )
        .context["cl"]
        .result_list
    )
    assert [w.pk for w in result] == [soon.pk]


def test_support_staff_can_register_but_not_delete(support_staff_client: Client) -> None:
    response = support_staff_client.post(f"{URL}add/", _form(residence_records=[_residence_row()]))
    assert response.status_code == 302
    worker = ForeignWorker.objects.get()
    assert support_staff_client.get(f"{URL}{worker.pk}/delete/").status_code == 403


def test_user_without_role_cannot_see_workers() -> None:
    client = verified_client(make_user("norole"))
    assert client.get(URL, follow=True).status_code == 403


def test_card_number_never_appears_in_audit_log(admin_client: Client) -> None:
    admin_client.post(f"{URL}add/", _form(residence_records=[_residence_row()]))
    record = ResidenceRecord.objects.get()
    log = AuditLog.objects.get(
        action=AuditAction.CREATE,
        content_type=ContentType.objects.get_for_model(ResidenceRecord),
        object_id=str(record.pk),
    )
    assert log.changes["card_number"] == [None, audit.MASK]
    assert log.changes["card_number_index"] == [None, audit.MASK]
    dumped = json.dumps(list(AuditLog.objects.values_list("changes", flat=True)))
    assert "AB12345678CD" not in dumped
    assert "gAAAA" not in dumped


def test_country_master_is_listed(admin_client: Client) -> None:
    assert admin_client.get("/admin/core/country/").status_code == 200
    assert Country.objects.filter(code__in=["CN", "VN", "ID", "PH"]).count() == 4


def test_add_and_change_pages_render_with_history_inlines(admin_client: Client) -> None:
    assert admin_client.get(f"{URL}add/").status_code == 200
    worker = make_worker()
    add_residence(worker)
    html = admin_client.get(f"{URL}{worker.pk}/change/").content.decode()
    assert 'value="AB12345678CD"' in html  # 1 行の入力欄で、復号して表示（閲覧は監査ログに残る）
    assert "AB12345678CD</textarea>" not in html
    assert "取り消す" in html
