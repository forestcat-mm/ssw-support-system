"""seed_dummy の段階2（T26）: docs/dummy-data.md の「段階2の場面」の表のとおりに入っていること、
seed を流した直後のダッシュボードと外国人ごとの警告が、意図したものだけであること。

外国人の番号は 1 から（docs/dummy-data.md の表の番号）。基準日は 2026-10-16。
"""

from collections import Counter
from datetime import date, timedelta
from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command
from pytest_django.fixtures import Settings

from apps.contracts.models import EmploymentContract
from apps.core.models import Attachment, User
from apps.filings.models import Filing, FilingKind, FilingStatus
from apps.seed.context import DUMMY_USERNAME, DUMMY_USERNAME_2
from apps.seed.models import SeedObject
from apps.support.models import (
    Briefing,
    BriefingCheck,
    Consultation,
    ConsultationStatus,
    DocumentDelivery,
    PeriodicInterview,
    SupportPlan,
    SupportRecord,
)
from apps.workers.models import ForeignWorker
from services import attachments, briefings, dashboard, deliveries, support_interviews

pytestmark = pytest.mark.django_db

BASE = date(2026, 10, 16)


@pytest.fixture(autouse=True)
def _seeded(settings: Settings) -> None:
    settings.DEBUG = True
    settings.ALLOW_DUMMY_DATA = True
    call_command("seed_dummy", "--date", BASE.isoformat(), stdout=StringIO())


def workers() -> list[ForeignWorker]:
    ids = SeedObject.objects.filter(content_type__model="foreignworker").values("object_id")
    return list(ForeignWorker.objects.filter(pk__in=ids).order_by("pk"))


def worker(number: int) -> ForeignWorker:
    return workers()[number - 1]


def numbers() -> dict[int, int]:
    return {w.pk: index for index, w in enumerate(workers(), start=1)}


# --- 意図しない警告が無い ----------------------------------------------------------------------

# seed 直後のダッシュボード（外国人の番号〔自機関・受入れ機関は 0〕, 種別, 警告レベル）
EXPECTED_DASHBOARD = [
    (0, "membership_renewal", "open"),  # 食品の会社の加入証明（D+25）
    (0, "registration_renewal", "open"),  # 自機関の登録更新
    (0, "support_contract_renewal", "open"),  # 精機の会社の支援委託契約（D+60）
    (1, "contract_change", "within_14"),  # 減給
    (1, "periodic_interview", "overdue"),
    (1, "residence_renewal", "overdue"),
    (2, "passport_check", "open"),
    (2, "periodic_interview", "within_7"),
    (2, "residence_renewal", "within_7"),
    (3, "periodic_interview", "within_14"),
    (3, "residence_renewal", "within_14"),
    (4, "contract_termination", "within_14"),  # 自己都合で離職
    (5, "periodic_interview", "open"),
    (5, "residence_renewal", "open"),
    (6, "acceptance_difficulty", "within_7"),  # 会社都合で離職
    (6, "contract_termination", "within_7"),
    (6, "support_difficulty", "within_14"),  # 非自発的離職の転職支援 → 第4-3号
    (7, "passport_check", "open"),
    (7, "periodic_interview", "open"),
    (7, "support_difficulty", "within_14"),  # 同じ日の閉じたタスクの後の新しいタスク
    (8, "periodic_interview", "open"),  # 取り消した組の後、元のタスクに戻る
    (9, "periodic_interview", "open"),
    (10, "contract_expiry_check", "open"),
    (10, "periodic_interview", "overdue"),
    (11, "periodic_interview", "within_7"),
    (12, "passport_check", "open"),
    (12, "periodic_interview", "within_14"),
    (13, "periodic_interview", "open"),
    (14, "periodic_interview", "open"),
    (15, "contract_change", "within_14"),  # 減給
    (15, "periodic_interview", "open"),
    (16, "periodic_interview", "overdue"),
    (17, "periodic_interview", "within_7"),
    (18, "periodic_interview", "within_14"),
    (19, "periodic_interview", "open"),
    (20, "periodic_interview", "open"),
    (21, "periodic_interview", "open"),
    (22, "periodic_interview", "within_7"),
    (23, "periodic_interview", "within_14"),
    (24, "periodic_interview", "open"),
    (26, "periodic_interview", "open"),
    (27, "periodic_interview", "open"),
    (28, "periodic_interview", "open"),
    (29, "periodic_interview", "open"),
    (30, "periodic_interview", "open"),
]

# 外国人ごとの警告（場面の表の意図したものだけ）
EXPECTED_WARNINGS = {
    9: [("career_plan", "career_plan_missing")],  # 転籍の新しい契約が未交付
    10: [("pre_guidance", "below_guideline"), ("pre_guidance", "unsigned")],
    14: [("orientation", "below_guideline"), ("orientation", "unsigned")],
    15: [("career_plan", "career_plan_missing")],  # 食品の契約で未交付
    # 最後の定期面談の本人の行がオンラインで、計画の実施方法は対面だけ（T35-D8）
    5: [
        (
            "interview",
            "オンラインで実施しましたが、支援計画の定期面談の実施方法にオンラインがありません。"
            "計画と本人の同意を確かめてください。",
        )
    ],
}


def test_dashboard_has_only_the_intended_rows() -> None:
    number = numbers()
    rows = dashboard.dashboard(dashboard.DashboardQuery(), BASE).rows
    found = sorted(
        (number.get(row.filing.worker_id or 0, 0), str(row.filing.kind), str(row.level))
        for row in rows
    )
    assert found == EXPECTED_DASHBOARD


def test_workers_have_only_the_intended_warnings() -> None:
    found: dict[int, list[tuple[str, str]]] = {}
    for index, w in enumerate(workers(), start=1):
        items: list[tuple[str, str]] = []
        items += [("career_plan", f.warning.code) for f in deliveries.career_plan_findings(w)]
        for delivery in w.document_deliveries.all():
            items += [("delivery", x.code) for x in deliveries.warnings(delivery)]
        for briefing in briefings.briefings_of(w):
            items += [(briefing.record.kind, x.code) for x in briefings.warnings(briefing)]
        for interview in support_interviews.interviews_of(w):
            items += [("interview", x) for x in support_interviews.warnings(interview)]
        if items:
            found[index] = items
    assert found == EXPECTED_WARNINGS


def test_interview_due_dates_are_spread() -> None:
    levels = Counter(
        str(row.level)
        for row in dashboard.dashboard(dashboard.DashboardQuery(), BASE).rows
        if row.filing.kind == FilingKind.PERIODIC_INTERVIEW
    )
    assert levels == {"overdue": 3, "within_7": 4, "within_14": 4, "open": 16}


# --- 場面ごと ---------------------------------------------------------------------------------


def test_plans_and_interviews() -> None:
    assert SupportPlan.objects.filter(is_template=False).count() == 32  # 9 番は 2 社分。31 番も
    assert PeriodicInterview.objects.filter(voided_at__isnull=True).count() >= 27
    voided = PeriodicInterview.objects.get(voided_at__isnull=False)
    assert voided.worker == worker(8)
    reported = PeriodicInterview.objects.get(report_required=True)
    assert (reported.worker, reported.reported_on) == (worker(7), BASE - timedelta(days=5))
    for number in (4, 6, 25):  # 離職・入国前は定期面談なし
        assert not PeriodicInterview.objects.filter(worker=worker(number)).exists()


def test_form_4_3_tasks() -> None:
    seven = Filing.objects.filter(kind=FilingKind.SUPPORT_DIFFICULTY, worker=worker(7))
    assert sorted((f.basis_on, f.status) for f in seven) == [
        (BASE - timedelta(days=5), FilingStatus.NOT_STARTED),
        (BASE - timedelta(days=5), FilingStatus.SUBMITTED),
    ]
    new = seven.get(status=FilingStatus.NOT_STARTED)
    assert [key.rsplit(":", 1)[-1] for key in new.trigger_details["sources"]] == [
        "report",
        "hello_work",
    ]
    [six] = Filing.objects.filter(kind=FilingKind.SUPPORT_DIFFICULTY, worker=worker(6))
    assert six.trigger_details["sources"][0].startswith("support_record:")
    job_changes = SupportRecord.objects.filter(kind="job_change")
    assert {(r.worker, r.details["separation"]) for r in job_changes} == {
        (worker(6), "involuntary"),
        (worker(4), "voluntary"),
    }
    assert not Filing.objects.filter(kind=FilingKind.SUPPORT_DIFFICULTY, worker=worker(4)).exists()


def test_consultations() -> None:
    cases = {numbers()[c.worker_id]: c for c in Consultation.objects.all()}
    assert len(cases) == 8
    assert len({c.category for c in cases.values()}) == 6
    assert {c.status for c in cases.values()} == set(ConsultationStatus.values)
    assert cases[7].reported and cases[7].hello_work
    assert cases[11].status == ConsultationStatus.HANDED_OVER and cases[11].handed_over_to
    reopened = [e.status_after for e in cases[13].entries.order_by("occurred_on")]
    assert reopened == [ConsultationStatus.RESOLVED, ConsultationStatus.IN_PROGRESS]


def test_guidance_and_orientation() -> None:
    def of(number: int) -> Briefing:
        return Briefing.objects.get(worker=worker(number))

    complete = of(25)
    assert complete.record.kind == "pre_guidance" and complete.fees.count() == 1
    assert briefings.warnings(complete) == []
    na = BriefingCheck.objects.filter(briefing=of(12), status="not_applicable").count()
    assert na == 2 and briefings.warnings(of(12)) == []
    for number in (25, 12):  # 署名済みの確認書あり
        assert briefings.is_signed(of(number))
    for number in (10, 14):  # 署名済みの確認書なし
        assert not briefings.is_signed(of(number))


def test_deliveries_and_transfer() -> None:
    transferred = EmploymentContract.objects.filter(worker=worker(9)).order_by("current_starts_on")
    old, new = transferred
    assert old.terminated_on == BASE - timedelta(days=60)
    assert new.host_organization.name == "（ダミー）サンプル水産加工株式会社"
    assert DocumentDelivery.objects.filter(contract=old, kind="career_up_plan").exists()
    assert not DocumentDelivery.objects.filter(contract=new).exists()
    assert not DocumentDelivery.objects.filter(worker=worker(15), kind="career_up_plan").exists()


def test_health_documents_for_two_staff_members() -> None:
    tanto = User.objects.get(username=DUMMY_USERNAME)
    tanto2 = User.objects.get(username=DUMMY_USERNAME_2)
    assert worker(1).assigned_staff.user == tanto  # type: ignore[union-attr]
    assert worker(2).assigned_staff.user == tanto2  # type: ignore[union-attr]

    def documents(number: int) -> list[Attachment]:
        return list(Attachment.objects.filter(object_id=worker(number).pk, is_sensitive=True))

    assert len(documents(1)) == 2 and len(documents(2)) == 1
    assert all(attachments.can_download(tanto, a) for a in documents(1))
    assert not any(attachments.can_download(tanto2, a) for a in documents(1))
    assert all(attachments.can_download(tanto2, a) for a in documents(2))
    assert not any(attachments.can_download(tanto, a) for a in documents(2))


def test_reset_recreates_the_same_support_data(settings: Settings) -> None:
    def counts() -> dict[str, Any]:
        return {
            "plans": SupportPlan.objects.count(),
            "interviews": PeriodicInterview.objects.count(),
            "records": SupportRecord.objects.count(),
            "consultations": Consultation.objects.count(),
            "briefings": Briefing.objects.count(),
            "deliveries": DocumentDelivery.objects.count(),
            "sensitive": Attachment.objects.filter(is_sensitive=True).count(),
            "filings": sorted(Counter((f.kind, f.status) for f in Filing.objects.all()).items()),
        }

    before = counts()
    call_command("seed_dummy", "--reset", "--date", BASE.isoformat(), stdout=StringIO())
    assert counts() == before
