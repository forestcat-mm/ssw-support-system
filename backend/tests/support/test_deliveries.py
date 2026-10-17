"""交付記録（T22、ADR-0010）: 入力の検査・取消・署名済みの控え・キャリアアッププランの判定
（雇用契約ごと・分野で判定・締結日との比較）・画面。"""

from datetime import date
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from apps.core.models import AttachmentKind, AuditAction, AuditLog
from apps.support.models import DocumentDelivery
from services import attachments, corporate_number, deliveries, support_plans, workers
from services.deliveries import DeliveryInput
from tests.conftest import PDF_BYTES
from tests.contracts.factories import make_contract, site_with_categories
from tests.core.factories import make_staff
from tests.hosts.factories import VALID_NUMBERS, category, make_host, make_site
from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 17)


def food_site(name: str = "食品工業株式会社", number: int = 1) -> Any:
    """飲食料品製造業分野の事業所（受入れ機関ごとに法人番号を変える）。"""
    base = f"{number:012d}"
    corporate = VALID_NUMBERS[1] if number == 1 else f"{corporate_number.check_digit(base)}{base}"
    return make_site(
        make_host(name=name, corporate_number=corporate), field_code="food_manufacturing"
    )


@pytest.fixture
def worker() -> Any:
    return make_worker(assigned_staff=make_staff(name="佐藤"))


@pytest.fixture
def food_contract(worker: Any) -> Any:
    return make_contract(worker, site=food_site(), signed_on=date(2026, 3, 1))


def career_plan(contract: Any, **overrides: Any) -> DeliveryInput:
    values: dict[str, Any] = {
        "kind": "career_up_plan",
        "delivered_on": date(2026, 2, 20),
        "method": "hand",
        "delivered_by": "host",
        "explained": True,
        "contract": contract,
    }
    return DeliveryInput(**(values | overrides))


def create(worker: Any, data: DeliveryInput, **kwargs: Any) -> DocumentDelivery:
    kwargs.setdefault("today", TODAY)
    return deliveries.create_delivery(worker, data, by=None, **kwargs)


def pdf(name: str = "控え.pdf") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, PDF_BYTES, content_type="application/pdf")


def keys(error: pytest.ExceptionInfo[ValidationError]) -> set[str]:
    return set(error.value.error_dict)


def codes(delivery: DocumentDelivery) -> list[str]:
    return [warning.code for warning in deliveries.warnings(delivery)]


def finding_codes(worker: Any) -> list[tuple[int, str]]:
    return [(f.contract.pk, f.warning.code) for f in deliveries.career_plan_findings(worker)]


# --- 記録 -------------------------------------------------------------------------------


def test_create_career_plan_with_a_signed_copy(worker: Any, food_contract: Any) -> None:
    delivery = create(worker, career_plan(food_contract, title=" 2026年版 "), files=[pdf()])
    assert (delivery.kind, delivery.title, delivery.contract) == (
        "career_up_plan",
        "2026年版",
        food_contract,
    )
    attachment = delivery.attachments.get()
    assert attachment.kind == AttachmentKind.DELIVERED_DOCUMENT
    assert deliveries.has_receipt(delivery)
    assert codes(delivery) == []  # 契約の前に交付・説明あり・控えあり
    assert AuditLog.objects.filter(
        action=AuditAction.CREATE, content_type__model="documentdelivery"
    ).exists()


def test_validate_fields(worker: Any, food_contract: Any) -> None:
    with pytest.raises(ValidationError) as error:
        create(
            worker,
            DeliveryInput(
                kind="other",
                delivered_on=date(2026, 10, 18),
                method="other",
                delivered_by="nobody",
            ),
        )
    assert keys(error) == {"title", "delivered_on", "method_note", "delivered_by"}
    for kind in ("career_up_plan", "experience_certificate", "employment_contract_copy"):
        with pytest.raises(ValidationError) as error:
            create(worker, career_plan(None, kind=kind))
        assert keys(error) == {"contract"}
    other_contract = make_contract(make_worker(), site=site_with_categories())
    with pytest.raises(ValidationError) as error:
        create(worker, career_plan(other_contract))
    assert keys(error) == {"contract"}
    with pytest.raises(ValidationError) as error:
        create(worker, career_plan(food_contract, requested_on=date(2026, 2, 1)))
    assert keys(error) == {"requested_on"}  # 求めを受けた日は実務経験証明書だけ
    with pytest.raises(ValidationError) as error:
        create(
            worker,
            career_plan(
                food_contract,
                kind="experience_certificate",
                delivered_on=date(2026, 9, 1),
                requested_on=date(2026, 9, 2),
            ),
        )
    assert keys(error) == {"requested_on"}  # 交付より後に求められたことにはできない
    workers.deactivate(worker, by=None, reason="重複")
    with pytest.raises(ValidationError) as error:
        create(worker, career_plan(food_contract))
    assert "__all__" in keys(error)


def test_experience_certificate_needs_no_explanation_or_copy(
    worker: Any, food_contract: Any
) -> None:
    delivery = create(
        worker,
        career_plan(
            food_contract,
            kind="experience_certificate",
            delivered_on=date(2026, 9, 10),
            requested_on=date(2026, 9, 1),
            explained=False,
            method="electronic",
        ),
    )
    assert delivery.requested_on == date(2026, 9, 1)
    assert codes(delivery) == []


def plan_copy(delivered_on: date) -> DeliveryInput:
    return DeliveryInput(
        kind="support_plan_copy",
        delivered_on=delivered_on,
        method="hand",
        delivered_by="organization",
    )


def test_support_plan_copy_takes_the_plan_version_of_the_day(worker: Any) -> None:
    contract = make_contract(worker, site=site_with_categories())
    plan = support_plans.create_plan(
        worker, contract.host_organization, applies_from=date(2026, 3, 20), by=None
    )
    delivery = create(worker, plan_copy(date(2026, 3, 25)))
    assert delivery.plan_version == support_plans.latest_version(plan)
    assert set(codes(delivery)) == {"not_explained", "no_receipt"}


def test_support_plan_copy_without_a_plan_cannot_be_saved(worker: Any) -> None:
    """交付日に適用されていた支援計画の版が無ければ、写しの対象が特定できないので保存できない。"""
    contract = make_contract(worker, site=site_with_categories())
    with pytest.raises(ValidationError) as error:
        create(worker, plan_copy(date(2026, 4, 10)))  # 計画が無い
    assert keys(error) == {"delivered_on"}
    assert error.value.error_dict["delivered_on"][0].code == "no_plan"
    support_plans.create_plan(
        worker, contract.host_organization, applies_from=date(2026, 5, 1), by=None
    )
    with pytest.raises(ValidationError) as error:
        create(worker, plan_copy(date(2026, 4, 30)))  # 計画の適用開始より前の日
    assert keys(error) == {"delivered_on"}
    assert not DocumentDelivery.objects.exists()
    assert create(worker, plan_copy(date(2026, 5, 1))).plan_version is not None


def test_void_requires_a_reason_and_keeps_the_row(worker: Any, food_contract: Any) -> None:
    delivery = create(worker, career_plan(food_contract))
    with pytest.raises(ValidationError):
        deliveries.void_delivery(delivery, by=None, reason=" ")
    deliveries.void_delivery(delivery, by=None, reason="別の人の記録")
    delivery.refresh_from_db()
    assert delivery.voided_at is not None and delivery.void_reason == "別の人の記録"
    assert codes(delivery) == []  # 取り消した記録には警告を出さない
    with pytest.raises(ValidationError):
        deliveries.void_delivery(delivery, by=None, reason="もう一度")
    with pytest.raises(ValidationError):
        deliveries.add_receipts(delivery, [pdf()], by=None)


# --- 署名済みの控え（T21 と同じ規則: 取り消していない添付があるか） -----------------------------


def test_receipt_is_an_active_attachment(worker: Any, food_contract: Any) -> None:
    delivery = create(worker, career_plan(food_contract), files=[pdf()])
    assert deliveries.has_receipt(delivery) and "no_receipt" not in codes(delivery)
    attachments.void_attachment(delivery.attachments.get(), by=None, reason="別の書類")
    assert not deliveries.has_receipt(delivery)
    assert "no_receipt" in codes(delivery)
    deliveries.add_receipts(delivery, [pdf("控え2.pdf")], by=None)
    assert deliveries.has_receipt(delivery)
    # 一覧で prefetch した添付を渡しても、同じ判定になる
    listed = deliveries.deliveries_of(worker).get(pk=delivery.pk)
    assert deliveries.has_receipt(listed, listed.attachments.all())


# --- キャリアアッププランの判定（雇用契約ごと） ------------------------------------------------


@pytest.mark.parametrize(
    ("delivered_on", "expected"),
    [
        (date(2026, 2, 28), []),
        (date(2026, 3, 1), []),  # 締結日と同じ日は警告しない
        (date(2026, 3, 2), ["after_signing"]),
    ],
)
def test_career_plan_must_be_given_before_the_contract(
    worker: Any, food_contract: Any, delivered_on: date, expected: list[str]
) -> None:
    delivery = create(worker, career_plan(food_contract, delivered_on=delivered_on), files=[pdf()])
    assert codes(delivery) == expected
    assert finding_codes(worker) == [(food_contract.pk, code) for code in expected]
    if expected:
        assert all(w.strong for w in deliveries.warnings(delivery))


def test_missing_date_of_conclusion_is_a_warning(worker: Any) -> None:
    contract = make_contract(worker, site=food_site())  # 締結日が未入力
    delivery = create(worker, career_plan(contract), files=[pdf()])
    assert codes(delivery) == ["no_signed_on"]
    assert finding_codes(worker) == [(contract.pk, "no_signed_on")]


def test_findings_are_per_contract_and_ignore_the_previous_contract(
    worker: Any, food_contract: Any
) -> None:
    """転籍後の新しい契約では、前の契約のキャリアアッププランの交付記録を使わない。"""
    assert finding_codes(worker) == [(food_contract.pk, "career_plan_missing")]
    create(worker, career_plan(food_contract))
    assert finding_codes(worker) == []

    new_contract = make_contract(
        worker,
        site=food_site("水産加工株式会社", number=2),
        changed_on=date(2027, 4, 1),
        starts_on=date(2027, 4, 1),
        ends_on=date(2028, 3, 31),
        signed_on=date(2027, 3, 1),
    )
    assert finding_codes(worker) == [(new_contract.pk, "career_plan_missing")]
    delivery = create(
        worker,
        career_plan(new_contract, delivered_on=date(2027, 2, 20)),
        today=date(2027, 6, 1),
    )
    assert finding_codes(worker) == []
    deliveries.void_delivery(delivery, by=None, reason="誤り")
    assert finding_codes(worker) == [(new_contract.pk, "career_plan_missing")]  # 取り消すと未交付


def test_field_decides_the_target_not_the_category(worker: Any) -> None:
    """2027-04-01 に業務区分が分かれても、飲食料品製造業分野の事業所の契約は対象のまま。"""
    industrial = make_contract(worker, site=site_with_categories(), signed_on=date(2026, 3, 1))
    assert finding_codes(worker) == []
    assert not deliveries.is_food_contract(industrial)
    seafood = make_contract(
        worker,
        site=food_site(number=2),
        changed_on=date(2027, 4, 1),
        starts_on=date(2027, 4, 1),
        ends_on=date(2028, 3, 31),
        signed_on=date(2027, 3, 1),
    )
    version = seafood.versions.get()
    version.categories.set([category("fishery_processing")])
    assert deliveries.is_food_contract(seafood)
    assert finding_codes(worker) == [(seafood.pk, "career_plan_missing")]
    # 工業製品製造業分野の契約にキャリアアッププランを記録しても、時期の判定はしない
    delivery = create(worker, career_plan(industrial, delivered_on=date(2026, 3, 5)))
    assert "after_signing" not in codes(delivery)


# --- 画面 -------------------------------------------------------------------------------


def list_url(worker: Any) -> str:
    return f"/workers/{worker.pk}/support/deliveries/"


def post_career_plan(client: Client, worker: Any, contract: Any, **overrides: Any) -> Any:
    url = f"/workers/{worker.pk}/support/deliveries/new/career_up_plan/"
    page = client.get(url)
    assert page.status_code == 200
    data: dict[str, Any] = {
        "once_token": page.context["once_token"],
        "delivered_on": "2026-02-20",
        "method": "hand",
        "delivered_by": "host",
        "delivered_by_name": "人事部 田中",
        "contract": contract.pk,
        "explained": "on",
        "uploads": [pdf()],
    }
    data.update(overrides)
    return client.post(url, data)


def test_list_shows_findings_and_kinds(
    support_staff_client: Client, worker: Any, food_contract: Any
) -> None:
    html = support_staff_client.get(list_url(worker)).content.decode()
    assert 'id="career-plan-findings"' in html and 'data-warning="career_plan_missing"' in html
    assert "/support/deliveries/new/experience_certificate/" in html
    assert "まだ交付の記録はありません。" in html
    entry = AuditLog.objects.filter(action=AuditAction.VIEW, object_id=str(worker.pk)).latest("pk")
    assert entry.context["tab"] == "support"


def test_form_fields_follow_the_kind(support_staff_client: Client, worker: Any) -> None:
    base = f"/workers/{worker.pk}/support/deliveries/new/"
    experience = support_staff_client.get(base + "experience_certificate/").content.decode()
    assert 'name="requested_on"' in experience and 'name="contract"' in experience
    copy_form = support_staff_client.get(base + "support_plan_copy/").content.decode()
    assert 'name="contract"' not in copy_form and 'name="requested_on"' not in copy_form
    assert support_staff_client.get(base + "guidance_confirmation/").status_code == 404


def test_create_attach_and_void_on_screen(
    support_staff_client: Client, worker: Any, food_contract: Any
) -> None:
    response = post_career_plan(support_staff_client, worker, food_contract)
    delivery = DocumentDelivery.objects.get(worker=worker)
    detail_url = f"/workers/{worker.pk}/support/deliveries/{delivery.pk}/"
    assert response["Location"] == detail_url
    assert delivery.delivered_by_name == "人事部 田中" and delivery.attachments.count() == 1
    html = support_staff_client.get(list_url(worker)).content.decode()
    assert 'id="career-plan-findings"' not in html
    assert f'data-delivery="{delivery.pk}"' in html and 'data-receipt="true"' in html

    # 控えを取り消すと、一覧も詳細も「控えなし」になる（同じ関数で判定）
    attachment = delivery.attachments.get()
    void_url = f"{detail_url}attachments/{attachment.pk}/void/"
    confirm = support_staff_client.get(void_url)
    support_staff_client.post(
        void_url, {"once_token": confirm.context["once_token"], "reason": "別の書類"}
    )
    assert 'data-receipt="false"' in support_staff_client.get(list_url(worker)).content.decode()
    detail = support_staff_client.get(detail_url).content.decode()
    assert "署名済みの控え・受領の記録が添付されていません。" in detail
    assert 'id="delivery-attachments" data-receipt="false"' in detail

    confirm = support_staff_client.get(detail_url + "void/")
    response = support_staff_client.post(
        detail_url + "void/", {"once_token": confirm.context["once_token"], "reason": "誤り"}
    )
    assert response["Location"] == detail_url
    html = support_staff_client.get(list_url(worker)).content.decode()
    assert f'data-delivery="{delivery.pk}" class="is-voided"' in html
    assert 'data-warning="career_plan_missing"' in html
    assert support_staff_client.get(detail_url + "void/").status_code == 404


def test_after_signing_is_shown_on_screen(
    support_staff_client: Client, worker: Any, food_contract: Any
) -> None:
    post_career_plan(support_staff_client, worker, food_contract, delivered_on="2026-03-02")
    delivery = DocumentDelivery.objects.get(worker=worker)
    html = support_staff_client.get(
        f"/workers/{worker.pk}/support/deliveries/{delivery.pk}/"
    ).content.decode()
    assert "雇用契約の締結（2026-03-01）より後に交付されています。" in html


def test_errors_are_shown_under_fields(
    support_staff_client: Client, worker: Any, food_contract: Any
) -> None:
    response = post_career_plan(
        support_staff_client, worker, food_contract, delivered_on="2099-01-01", method="other"
    )
    html = response.content.decode()
    assert 'id="id_delivered_on-error"' in html and 'id="id_method_note-error"' in html
    assert not DocumentDelivery.objects.exists()


def test_deactivated_worker_deliveries_are_read_only(
    admin_client: Client, worker: Any, food_contract: Any
) -> None:
    delivery = create(worker, career_plan(food_contract))
    workers.deactivate(worker, by=None, reason="重複")
    html = admin_client.get(list_url(worker)).content.decode()
    assert "/deliveries/new/" not in html and "/void/" not in html
    detail_url = f"/workers/{worker.pk}/support/deliveries/{delivery.pk}/"
    assert admin_client.get(detail_url).status_code == 200
    assert admin_client.get(detail_url + "void/").status_code == 403
    url = f"/workers/{worker.pk}/support/deliveries/new/career_up_plan/"
    assert admin_client.get(url).status_code == 403
