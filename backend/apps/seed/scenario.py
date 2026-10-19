"""手動のテストシナリオの本線の記録（seed_scenario）。

シナリオは Claude Docs「段階3 通しの手動テストシナリオ」。

王 麗娜さん（WANG LINA）と、さくら台フーズ株式会社の記録を、シナリオの節ごとに作る。
- `until` は「節 1〜N が終わった状態」。節 9（変更・終了・取消）は画面で試す手順なので作らない。
- わざと誤る手順（個人事業主の法人番号・内訳の合計の誤り・取り消す公的手続の行）は作らず、
  直した後の正しい状態だけを作る。
- 節 6 の生成物と署名済みの添付は `with_docs` のときだけ作る（LibreOffice の変換が重いため）。
  付けないときは、生成の手前（記録がそろった状態）で止める。キャリアアッププランの交付記録・
  面談の報告書・相談記録書も同じ。
- 担当者は seed_dummy の dummy_tanto（佐藤 花子）。seed_dummy を先に流す。
- 作った行はダミーの台帳（SeedObject）に scope="scenario" で載せる。消すのは reset() だけで、
  seed_dummy のダミーデータは消さない。
- 生成と署名は、その場面の日付の時刻で行う（as_of。署名日は生成日より前にできないため）。
業務のルール（検査・タスクの起票）は services をそのまま呼ぶ。値はシナリオの「本線のダミーデータ
一覧」のとおり。値の無い欄は空にする。作れなかった値は NOT_STORED に書く。
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from django.contrib.contenttypes.models import ContentType
from django.core.files.base import ContentFile
from django.db import models, transaction
from django.utils import timezone

from apps.contracts.models import (
    EmploymentContract,
    EmploymentContractAllowance,
    EmploymentContractDeduction,
    EmploymentContractVersion,
    WageComparison,
    WageUnit,
)
from apps.core.models import (
    Attachment,
    AttachmentKind,
    Country,
    Organization,
    Staff,
    SupportLanguage,
    User,
)
from apps.forms_engine.models import GeneratedDocument
from apps.hosts.models import (
    FeeTiming,
    HostCooperationConfirmation,
    HostMembership,
    HostOrganization,
    HostSite,
    OperationCategory,
    SpecifiedSkillField,
    SupportContract,
)
from apps.seed import builders
from apps.seed.context import DUMMY_PDF, DUMMY_USERNAME
from apps.seed.models import SeedObject, SeedScope
from apps.support.models import (
    ConsultationStatus,
    InterviewPreference,
    PeriodicInterview,
)
from apps.workers.models import (
    ApplicationType,
    ForeignWorker,
    PassportRecord,
    ResidenceRecord,
    Sex,
    StatusOfResidence,
    WorkerAddress,
    WorkerContact,
    WorkerStatus,
)
from services import (
    briefings,
    career_plans,
    consultations,
    contracts,
    deliveries,
    form_data_1_17,
    form_data_application,
    form_data_briefing,
    form_data_career,
    form_data_consultation,
    form_data_contract,
    form_data_interview,
    form_generation,
    form_signatures,
    host_records,
    interview_answers,
    support_interviews,
    support_plans,
    support_records,
    wage_explanations,
    worker_documents,
    workers,
)
from services.attachments import create_attachment
from services.briefings import BriefingInput, CheckInput, SessionInput
from services.consultations import EntryInput, ReceptionInput
from services.deliveries import DeliveryInput
from services.support_catalog import SupportItem
from services.support_interviews import InterviewInput
from services.support_records import RecordInput

SECTIONS = {
    1: "自機関と担当者",
    2: "受入れ機関",
    3: "外国人",
    4: "雇用契約と報酬の説明",
    5: "支援計画と入国前の支援",
    6: "申請の書類",
    7: "入国後の支援",
    8: "定期面談と相談",
}
LAST_SECTION = max(SECTIONS)
TOKYO = ZoneInfo("Asia/Tokyo")
HOST_NAME = "さくら台フーズ株式会社"
WORKER_ROMAJI = "WANG LINA"
# 業務区分は 2026 年に選べる「飲食料品製造業全般」（「飲食料品製造業」は 2027-04-01 から）
FOOD_CATEGORY = "food_manufacturing_general"
AUTHOR = "代表取締役　高橋 誠"  # 第1-25号・第5-10号・第13-1号の作成責任者
SIGNED_ON = date(2026, 7, 8)  # 節 6 の署名済みの添付の署名日

# 値はあるが、今の作りに欄が無い・決まった形で入れられないもの（報告に書く）
NOT_STORED = (
    "外国人の出生地（中国河南省）: 外国人の台帳に出生地の欄が無い",
    "協議会の名称（食品産業特定技能協議会）: 加入の行に協議会の名称の欄が無い（分野で決まる）",
    "雇用契約の控除の税金・社会保険料・雇用保険料: 控除の行は額が必須で、額の値が無い",
    "報酬の説明の賃金規程の報酬（月給 205,000〜230,000 円）: 欄は 1 つの額なので "
    "205,000 円を入れた",
    "事前ガイダンスの「費用の合意 有」: 外国の機関に払った費用の行（相手・日・額）の値が無いので、"
    "費用は「払っていない」にした（合意の印は、払った費用があるときだけ入れられる）",
    "雇用条件書の雇入れ時の健康診断の日: 「雇入れ時」の日付の値が無いので空",
)


# 値に無いが、入力が必須なので決めて入れたもの（報告に書く）
FILLED = (
    "キャリアアッププランの適用開始日 2026-07-01（支援委託契約の開始日）と想定されるキャリアルート"
    "「一般 → 班長補佐 → 班長」（レベルの地位から）",
    "支援計画の適用開始日 2026-07-04（作成日と同じ）",
    "雇用契約の第1版の変更日（版の適用の始まり）2026-09-15（契約の開始日）",
    "業務区分「飲食料品製造業」は、2026 年に選べる「飲食料品製造業全般」にした"
    "（「飲食料品製造業」は 2027-04-01 からの区分）",
    "事前ガイダンスの本人確認の印（オンラインのガイダンスでは必須）",
    "2 回目の面談の ⑥ の対応: 本人を船橋労働基準監督署に案内／受入れ機関（製造部 製造課長 "
    "山本 健二）に 2026-10-17 に通知／出入国在留管理庁への案内は済み",
)


class ScenarioError(Exception):
    pass


@dataclass
class ScenarioContext:
    staff: Staff
    user: User
    created: list[models.Model] = field(default_factory=list)
    host: HostOrganization | None = None
    site: HostSite | None = None
    worker: ForeignWorker | None = None
    contract: EmploymentContract | None = None
    version: EmploymentContractVersion | None = None
    documents: dict[str, GeneratedDocument] = field(default_factory=dict)

    def keep(self, obj: models.Model) -> Any:
        SeedObject.scenario_objects.create(
            content_type=ContentType.objects.get_for_model(obj), object_id=obj.pk
        )
        self.created.append(obj)
        return obj


@contextmanager
def as_of(day: date, at: time = time(10)) -> Iterator[None]:
    """この中の「今」をその日の時刻にする（生成日・署名日の検査のため。抜けると元に戻す）。"""
    real = timezone.now
    offset = datetime.datetime.combine(day, at, tzinfo=TOKYO) - real()
    timezone.now = lambda: real() + offset
    try:
        yield
    finally:
        timezone.now = real


def _pdf(name: str) -> ContentFile[bytes]:
    return ContentFile(DUMMY_PDF, name=name)


def _language(code: str) -> SupportLanguage:
    return SupportLanguage.objects.get(code=code)


def exists() -> bool:
    return SeedObject.scenario_objects.exists()


# --- 節 1 自機関と担当者 ---------------------------------------------------------------------


def section_1(ctx: ScenarioContext) -> None:
    """空の項目だけを埋める（既存の値は変えない）。標準の支援計画は確かめるだけ。"""
    organization = Organization.objects.first()
    if organization is None:
        raise ScenarioError("Organization is not registered. Run seed_dummy first.")
    values: dict[str, Any] = {
        "support_start_planned_on": date(2024, 4, 1),
        "representative_title": "代表取締役",
        "representative_name": "小林 直人",
        "court_name": "東京地方裁判所",
    }
    changed = [name for name, value in values.items() if not getattr(organization, name)]
    for name in changed:
        setattr(organization, name, values[name])
    if not organization.signed_copy_due_days:
        organization.signed_copy_due_days = 14
        changed.append("signed_copy_due_days")
    if changed:
        organization.save(update_fields=[*changed, "updated_at"])
    ctx.staff.languages.add(_language("zh-hans"))


# --- 節 2 受入れ機関 -------------------------------------------------------------------------


LEVELS = (
    (
        "レベル1",
        1,
        2,
        "そう菜の製造ラインの作業、衛生管理の基本",
        "入社後の OJT",
        "一般",
        "年1回の昇給",
    ),
    (
        "レベル2",
        3,
        4,
        "ラインリーダーの補佐、新人への指導",
        "実務 2 年以上・飲食料品製造業特定技能2号評価試験",
        "班長補佐",
        "役職手当 月 5,000 円",
    ),
    (
        "レベル3",
        5,
        None,
        "ラインの工程管理",
        "班長補佐の経験 1 年以上・食品衛生責任者",
        "班長",
        "役職手当 月 10,000 円",
    ),
)


def section_2(ctx: ScenarioContext) -> None:
    food = SpecifiedSkillField.objects.get(code="food_manufacturing")
    host = HostOrganization(
        entity_type="corporation",
        name=HOST_NAME,
        name_kana="さくらだいふーずかぶしきがいしゃ",
        corporate_number="7123456789012",  # 「7-1234-5678-9012」を正規化した形
        representative_title="代表取締役",
        representative_name="高橋 誠",
        postal_code="273-0012",
        address="千葉県船橋市浜町四丁目10番1号",
        phone_number="047-400-1234",
    )
    ctx.host = ctx.keep(host_records.save_host(host, by=ctx.user))
    site = HostSite.objects.create(
        host_organization=host,
        name="本社工場",
        postal_code="273-0012",
        address="千葉県船橋市浜町四丁目10番1号",
        phone_number="047-400-1235",
        # 日本標準産業分類（令和5年7月改定）の細分類 0996 そう（惣）菜製造業
        industry_code="0996",
        field=food,
    )
    site.categories.set(OperationCategory.objects.filter(code=FOOD_CATEGORY))
    ctx.site = ctx.keep(site)
    membership = HostMembership.objects.create(
        host_organization=host,
        field=food,
        joined_on=date(2026, 6, 1),
        valid_until=date(2027, 5, 31),
        member_number="F-2026-01234",
    )
    ctx.keep(membership)
    ctx.keep(
        create_attachment(
            target=membership,
            kind=AttachmentKind.MEMBERSHIP_CERTIFICATE,
            file=_pdf("協議会構成員証明書.pdf"),
            created_by=ctx.user,
            valid_until=date(2027, 5, 31),
        )
    )
    for municipality, filename in (
        ("千葉県船橋市", "協力確認書控え_船橋市.pdf"),
        ("千葉県市川市", "協力確認書控え_市川市.pdf"),
    ):
        confirmation = HostCooperationConfirmation.objects.create(
            host_organization=host,
            site=site,
            municipality=municipality,
            submitted_on=date(2026, 6, 25),
        )
        ctx.keep(confirmation)
        ctx.keep(
            create_attachment(
                target=confirmation,
                kind=AttachmentKind.COOPERATION_CONFIRMATION,
                file=_pdf(filename),
                created_by=ctx.user,
            )
        )
    support = SupportContract(
        host_organization=host,
        concluded_on=date(2026, 6, 20),
        starts_on=date(2026, 7, 1),
        ends_on=date(2027, 6, 30),
        monthly_fee=30000,
    )
    host_records.save_support_contract(
        support,
        files=[_pdf("支援委託契約書.pdf")],
        by=ctx.user,
        today=date(2026, 6, 20),
        fees=[
            host_records.FeeInput("支援業務の実施", 25000, FeeTiming.REGULAR),
            host_records.FeeInput("通訳・翻訳", 5000, FeeTiming.REGULAR),
        ],
    )
    ctx.keep(support)
    for attachment in Attachment.objects.filter(
        content_type=ContentType.objects.get_for_model(SupportContract), object_id=support.pk
    ):
        ctx.keep(attachment)
    version = career_plans.create_plan(
        host,
        food,
        None,
        career_plans.VersionInput(
            applies_from=date(2026, 7, 1),  # 値に無い（支援委託契約の開始日にした）
            career_route="一般 → 班長補佐 → 班長",  # 値に無い（レベルの地位から作った）
            levels=[
                career_plans.LevelInput(
                    name=name,
                    years_from=years_from,
                    years_to=years_to,
                    duties=duties,
                    requirements=requirements,
                    position=position,
                    treatment=treatment,
                )
                for name, years_from, years_to, duties, requirements, position, treatment in LEVELS
            ],
        ),
        by=ctx.user,
    )
    ctx.keep(version.plan)
    ctx.keep(version)
    for level in version.levels.all():
        ctx.keep(level)


# --- 節 3 外国人 --------------------------------------------------------------------------


def section_3(ctx: ScenarioContext) -> None:
    zh = _language("zh-hans")
    worker = ForeignWorker(
        name_romaji=WORKER_ROMAJI,
        name_kanji="王 麗娜",
        name_kana=workers.to_katakana("ワン リナ"),
        nationality=Country.objects.get(code="CN"),
        birth_date=date(2001, 5, 14),
        sex=Sex.FEMALE,
        support_language=zh,
        assigned_staff=ctx.staff,
        status=WorkerStatus.PRE_ENTRY,
    )
    records: list[Any] = [
        PassportRecord(
            number="E87654321",
            issuing_country=Country.objects.get(code="CN"),
            issued_on=date(2023, 3, 10),
            expires_on=date(2033, 3, 9),
        ),
        # 本国の住所は連絡先の「本国の住所」（住所の行は日本の住所で、郵便番号と適用開始日が要る）
        WorkerContact(
            email="wang.lina@example.com",
            home_country_address="中国河南省鄭州市金水区花園路100号",
        ),
    ]
    workers.register_worker(worker, records, by=ctx.user)
    ctx.worker = ctx.keep(worker)
    for record in records:
        ctx.keep(record)


# --- 節 4 雇用契約と報酬の説明 ------------------------------------------------------------------


def section_4(ctx: ScenarioContext) -> None:
    assert ctx.host is not None and ctx.site is not None and ctx.worker is not None
    version = EmploymentContractVersion(
        changed_on=date(2026, 9, 15),
        signed_on=date(2026, 7, 3),
        starts_on=date(2026, 9, 15),
        ends_on=date(2027, 9, 14),
        expected_entry_on=date(2026, 9, 15),
        renewal="possible",
        renewal_criteria=["workload", "performance", "business"],
        has_renewal_limit=False,
        indefinite_conversion=False,
        site=ctx.site,
        duties="そう菜の製造・包装",
        start_time=time(8, 30),
        end_time=time(17, 30),
        daily_working_minutes=480,
        break_minutes=60,
        weekly_working_minutes=2400,
        monthly_working_minutes=9600,  # 月 160 時間（年 1,920 時間 ÷ 12）
        annual_working_minutes=115200,
        weekly_working_days=Decimal(5),
        annual_working_days=240,
        has_overtime=True,
        work_rules_articles_hours="第18条～第22条",
        regular_holiday_weekdays=["sat", "sun"],
        national_holidays_off=True,
        regular_holidays_other="年末年始（12月30日～1月3日）",
        annual_holidays=125,
        work_rules_articles_holidays="第23条～第24条",
        paid_leave_days=10,
        has_early_paid_leave=False,
        work_rules_articles_leave="第25条～第30条",
        wage_type="monthly",
        base_wage=210000,
        hourly_equivalent=1313,  # 210,000 ÷ 160 時間 ＝ 1,312.5 → 1,313 円
        overtime_rate_within_60h=Decimal(25),
        holiday_rate_statutory=Decimal(35),
        night_rate=Decimal(25),
        wage_closing_day="毎月末日",
        wage_payment_day="翌月25日",
        payment_method="bank_transfer",
        has_raise=True,
        raise_note="年1回（4月）",
        has_bonus=True,
        bonus_note="年2回（業績による）",
        has_retirement_allowance=False,
        resignation_notice_days=30,
        work_rules_articles_resignation="第40条～第45条",
        insurances=["employment", "workers_compensation", "health", "employees_pension"],
        periodic_health_check_interval="1年",
        treatment_contact_department="総務部",
        treatment_contact_person="田中 一郎",
        treatment_contact_phone="047-400-1236",
        consultation_contact_department="総務部",
        consultation_contact_person="田中 一郎",
        consultation_contact_phone="047-400-1236",
    )
    contracts.add_version(
        version, contract=None, worker=ctx.worker, host_organization=ctx.host, by=ctx.user
    )
    ctx.contract = ctx.keep(version.contract)
    ctx.version = ctx.keep(version)
    version.categories.set(OperationCategory.objects.filter(code=FOOD_CATEGORY))
    ctx.keep(
        EmploymentContractAllowance.objects.create(version=version, name="通勤手当", amount=10000)
    )
    for kind, name, amount in (("housing", "社宅費", 25000), ("utilities", "水道光熱費", 8000)):
        ctx.keep(
            EmploymentContractDeduction.objects.create(
                version=version, kind=kind, name=name, amount=amount
            )
        )
    contracts.complete_version(version, by=ctx.user)
    wage_explanations.save(
        version,
        {
            "comparison": WageComparison.COMPARABLE,
            "wage_unit": WageUnit.MONTHLY,
            "applicant_duties": "役職なし（製造スタッフ）。職務: そう菜の製造・包装。"
            "責任: 担当工程の品質確認",
            "applicant_experience_years": Decimal(0),
            "japanese_duties": "製造スタッフ",
            "japanese_age": 21,
            "japanese_sex": "male",
            "japanese_experience_years": Decimal(1),
            "japanese_wage": 210000,
            "has_wage_rules": True,
            "rules_wage": 205000,  # 規程の報酬は月給 205,000〜230,000 円（1 等級）。欄は 1 つの額
            "reason": "同じ職務・等級に同じ賃金表を適用している",
        },
        by=ctx.user,
    )


# --- 節 5 支援計画と入国前の支援 ----------------------------------------------------------------


def _checks(kind: SupportItem, on: date) -> dict[str, CheckInput]:
    checklist = briefings.checklist_for(kind, on)
    return {code: CheckInput("done") for code in checklist.item_codes}


def _generate(
    form_id: str, target: models.Model, data: dict[str, Any], on: date, **kwargs: Any
) -> GeneratedDocument:
    try:
        document = form_generation.request(
            form_id, target=target, data=data, basis_on=on, by=None, accept_warnings=True, **kwargs
        )
    except form_generation.GenerationBlocked as blocked:
        messages = "; ".join(str(finding.message) for finding in blocked.result.blocking)
        raise ScenarioError(f"{form_id} cannot be generated: {messages}") from blocked
    form_generation.run(document.pk)  # ジョブを待たずにその場で
    document.refresh_from_db()
    return document


def section_5(ctx: ScenarioContext, with_docs: bool) -> None:
    assert ctx.worker is not None and ctx.host is not None and ctx.contract is not None
    zh = _language("zh-hans")
    content = support_plans.content_for_worker(ctx.worker)
    content.created_on = date(2026, 7, 4)
    content.interview_preference = InterviewPreference.IN_PERSON
    content.translation_language_id = zh.pk
    support_plans.create_plan(
        ctx.worker, ctx.host, applies_from=date(2026, 7, 4), by=ctx.user, content=content
    )
    on = date(2026, 7, 5)
    briefings.create_briefing(
        ctx.worker,
        BriefingInput(
            kind=SupportItem.PRE_GUIDANCE,
            method="online",
            staff=ctx.staff,
            place="オンライン（ビデオ通話）",
            sessions=[SessionInput(on, time(10, 0), time(13, 30))],
            checks=_checks(SupportItem.PRE_GUIDANCE, on),
            languages=[zh],
            interpreter_kind="other",
            interpreter="陳 美玲",
            interpreter_language=zh,
            identity_confirmed=True,  # オンラインのガイダンスは本人確認の印が必須
            deposit_contract="none",
            foreign_fees="none",  # 費用の行の値が無い（NOT_STORED）
        ),
        by=ctx.user,
        today=on,
    )
    worker_documents.add_documents(
        ctx.worker,
        "health_check_record",
        [_pdf("健康診断個人票.pdf")],
        retention_until=None,
        examined_on=date(2026, 6, 25),
        by=ctx.user,
        today=date(2026, 6, 25),
    )
    delivered_on = date(2026, 7, 3)
    generated = None
    if with_docs:
        with as_of(delivered_on):
            generated = _generate(
                "career-plan",
                ctx.contract,
                form_data_career.build_career_plan(ctx.contract, delivered_on),
                delivered_on,
            )
        ctx.documents["career-plan"] = generated
    deliveries.create_delivery(
        ctx.worker,
        DeliveryInput(
            kind="career_up_plan",
            delivered_on=delivered_on,
            method="hand",
            delivered_by="organization",
            explained=True,
            contract=ctx.contract,
            languages=[_language("ja")],
            generated_document=generated,
        ),
        by=ctx.user,
        today=delivered_on,
    )


# --- 節 6 申請の書類（with_docs のときだけ） ---------------------------------------------------


def section_6(ctx: ScenarioContext, with_docs: bool) -> None:
    if not with_docs:
        return  # 生成の手前で止める（記録は節 5 までにそろっている）
    assert ctx.worker is not None and ctx.contract is not None and ctx.version is not None
    basis = date(2026, 7, 10)
    plan = ctx.worker.support_plans.get()
    plan_version = support_plans.latest_version(plan)
    assert plan_version is not None
    contract, version = ctx.contract, ctx.version
    with as_of(date(2026, 7, 7)):  # 署名（7/8）の前に生成する
        documents = {
            "1-17": _generate(
                "1-17",
                plan_version,
                form_data_1_17.build(plan_version, basis) | form_data_1_17.declaration_values(True),
                basis,
            ),
            "1-6": _generate("1-6", version, form_data_contract.build_1_6(version), basis),
            "1-5": _generate(
                "1-5", version, form_data_contract.build_1_5(version), basis, language="ja-zh"
            ),
            "1-4": _generate(
                "1-4", version, form_data_contract.build_1_4(version, basis, author=AUTHOR), basis
            ),
        }
        for form_id in ("13-1", "13-2", "1-25", "5-10"):
            author = (
                AUTHOR
                if form_id != "13-2"
                else form_data_application.default_author(form_id, contract)
            )
            documents[form_id] = _generate(
                form_id,
                contract,
                form_data_application.build(form_id, contract, basis, author=author),
                basis,
            )
        guidance = briefings.briefings_of(ctx.worker).get(record__kind=SupportItem.PRE_GUIDANCE)
        documents["5-9"] = _generate(
            "5-9",
            guidance,
            form_data_briefing.build(guidance),
            guidance.record.occurred_on,
            language="ja-zh",
        )
    ctx.documents.update(documents)
    with as_of(SIGNED_ON):
        for form_id in ("1-17", "1-5", "1-6", "5-9", "career-plan"):
            form_signatures.add_signature(
                ctx.documents[form_id],
                [_pdf(f"署名済み_{form_id}.pdf")],
                signed_on=SIGNED_ON,
                by=ctx.user,
                today=SIGNED_ON,
            )
    deliveries.create_delivery(
        ctx.worker,
        DeliveryInput(
            kind="support_plan_copy",
            delivered_on=basis,
            method="hand",
            delivered_by="organization",
            explained=True,
            languages=[_language("ja")],
            generated_document=ctx.documents["1-17"],
        ),
        by=ctx.user,
        today=basis,
    )


# --- 節 7 入国後の支援 ---------------------------------------------------------------------


def _record(ctx: ScenarioContext, kind: SupportItem, on: date, body: str, **values: Any) -> None:
    assert ctx.worker is not None
    support_records.create_record(
        ctx.worker,
        RecordInput(
            kind=kind,
            occurred_on=on,
            method="in_person",
            staff=ctx.staff,
            body=body,
            languages=[_language("zh-hans")],
            **values,
        ),
        by=ctx.user,
        today=on,
    )


def section_7(ctx: ScenarioContext, with_docs: bool) -> None:
    worker = ctx.worker
    assert worker is not None
    for record in (
        ResidenceRecord(
            card_number="",
            status_of_residence=StatusOfResidence.SSW_1,
            application_type=ApplicationType.CERTIFICATE_OF_ELIGIBILITY,
            permitted_on=date(2026, 8, 20),
            expires_on=date(2026, 11, 19),  # 認定証明書の有効期間
        ),
        # 上陸の許可で交付された在留カード（認定証明書で入国。在留カードの番号がある行は在留期限）
        ResidenceRecord(
            card_number="TK48261937EA",
            status_of_residence=StatusOfResidence.SSW_1,
            application_type=ApplicationType.CERTIFICATE_OF_ELIGIBILITY,
            permitted_on=date(2026, 9, 15),
            expires_on=date(2027, 9, 15),
        ),
        WorkerAddress(
            postal_code="272-0023",
            address="千葉県市川市南八幡五丁目8番8号 さくら台寮 203号室",
            effective_from=date(2026, 9, 15),
        ),
        # 連絡先は行で持つ（入国後の電話を足した新しい行。メールと本国の住所は前の行と同じ）
        WorkerContact(
            phone="080-0000-5678",
            email="wang.lina@example.com",
            home_country_address="中国河南省鄭州市金水区花園路100号",
        ),
    ):
        workers.add_record(worker, record, by=ctx.user)
        ctx.keep(record)
    worker.status = WorkerStatus.RESIDING
    worker.save(update_fields=["status", "updated_at"])
    _record(
        ctx,
        SupportItem.AIRPORT_TRANSFER,
        date(2026, 9, 15),
        "成田空港からさくら台寮まで送迎した。",
        details={"direction": "2.a", "airport": "成田空港", "transport": "成田空港 → さくら台寮"},
    )
    _record(
        ctx,
        SupportItem.HOUSING_AND_CONTRACTS,
        date(2026, 9, 15),
        "寮の入居に立ち会った（居室 9.5㎡）。",
        details={"activities": ["3a.c"]},
    )
    _record(
        ctx,
        SupportItem.HOUSING_AND_CONTRACTS,
        date(2026, 9, 19),
        "銀行口座の開設と携帯電話の契約に同行した。",
        details={"activities": ["3b.a", "3b.b"]},
    )
    _record(
        ctx,
        SupportItem.PUBLIC_PROCEDURES,
        date(2026, 9, 18),
        "市川市役所で転入届とマイナンバーの手続に同行した。",
        details={"procedure": "転入届・マイナンバーの手続", "office": "市川市役所"},
    )
    _record(
        ctx,
        SupportItem.JAPANESE_LEARNING,
        date(2026, 9, 25),
        "市川市の地域の日本語教室を案内した。",
        details={"ways": ["5.a"]},
    )
    _record(
        ctx,
        SupportItem.EXCHANGE,
        date(2026, 10, 5),
        "地域の秋祭りを案内し、参加の手配をした。",
        details={"event": "地域の秋祭り", "exchange_type": "7.b"},
    )
    on = date(2026, 9, 16)
    zh = _language("zh-hans")
    orientation = briefings.create_briefing(
        worker,
        BriefingInput(
            kind=SupportItem.ORIENTATION,
            method="in_person",
            staff=ctx.staff,
            place="本社工場の会議室",
            sessions=[
                SessionInput(on, time(9, 0), time(12, 0)),
                SessionInput(on, time(13, 0), time(18, 0)),  # 休憩 1 時間、実 8 時間
            ],
            checks=_checks(SupportItem.ORIENTATION, on),
            languages=[zh],
            interpreter_kind="other",
            interpreter="陳 美玲",
            interpreter_language=zh,
        ),
        by=ctx.user,
        today=on,
    )
    if with_docs:
        with as_of(on, time(18, 30)):
            document = _generate(
                "5-8", orientation, form_data_briefing.build(orientation), on, language="ja-zh"
            )
            form_signatures.add_signature(
                document, [_pdf("署名済み_5-8.pdf")], signed_on=on, by=ctx.user, today=on
            )
        ctx.documents["5-8"] = document


# --- 節 8 定期面談と相談 --------------------------------------------------------------------


SUPERVISOR = {"supervisor_position": "製造課長", "supervisor_department": "製造部"}
RECORDING = r"社内ファイルサーバー\面談録画\2026\WANG_LINA_20261017"


def _interview_row(ctx: ScenarioContext, on: date, method: str, **values: Any) -> RecordInput:
    return RecordInput(
        kind=SupportItem.PERIODIC_INTERVIEW,
        occurred_on=on,
        method=method,
        staff=ctx.staff,
        body=values.pop("body", "仕事と生活の状況を確認した。"),
        languages=values.pop("languages", [_language("zh-hans")]),
        **values,
    )


def section_8(ctx: ScenarioContext, with_docs: bool) -> None:
    worker = ctx.worker
    assert worker is not None and ctx.host is not None
    first_on = date(2026, 10, 15)
    first = support_interviews.create_interview(
        worker,
        InterviewInput(
            self_row=_interview_row(ctx, first_on, "in_person"),
            supervisor_row=_interview_row(
                ctx,
                first_on,
                "in_person",
                counterpart_name="山本 健二",
                languages=[],
                body="勤務態度・労働時間を確認した。",
            ),
            self_answers=interview_answers.AnswerSetInput(
                answers=interview_answers.all_no(first_on)
            ),
            supervisor_answers=interview_answers.AnswerSetInput(
                answers=interview_answers.all_no(first_on), **SUPERVISOR
            ),
            nonconformity=interview_answers.NonconformityInput(exists=False),
        ),
        by=ctx.user,
        today=first_on,
    )
    second_on = date(2026, 10, 17)
    self_answers = interview_answers.all_no(second_on)
    self_answers["2.1"] = interview_answers.AnswerInput(
        True, "9月分の時間外手当の一部が支払われていない"
    )
    supervisor_answers = interview_answers.all_no(second_on)
    supervisor_answers["2.1"] = interview_answers.AnswerInput(True, "集計の誤りを確認中")
    second = support_interviews.create_interview(
        worker,
        InterviewInput(
            self_row=_interview_row(ctx, second_on, "online"),
            supervisor_row=_interview_row(
                ctx,
                second_on,
                "online",
                counterpart_name="山本 健二",
                languages=[],
                body="時間外手当の支払を確認した。",
            ),
            violation_suspected=True,
            report_required=True,
            report_to="labor_standards_office",
            report_to_note="船橋労働基準監督署",
            reported_on=date(2026, 10, 18),
            self_answers=interview_answers.AnswerSetInput(
                answers=self_answers, recording_location=RECORDING
            ),
            supervisor_answers=interview_answers.AnswerSetInput(
                answers=supervisor_answers, recording_location=RECORDING, **SUPERVISOR
            ),
            nonconformity=interview_answers.NonconformityInput(
                exists=True,
                occurred_on=date(2026, 9, 30),
                content="時間外手当の一部未払い（約 6 時間分）",
                # 値に無いが必須の欄（FILLED）。通報先・面談の相手に合わせた
                worker_guidance="guided",
                guided_to="船橋労働基準監督署",
                host_notified=True,
                notified_on=second_on,
                notified_to="製造部 製造課長 山本 健二",
                immigration_guided=True,
            ),
        ),
        by=ctx.user,
        today=date(2026, 10, 18),
    )
    case = consultations.create_consultation(
        worker,
        ReceptionInput(
            record=RecordInput(
                kind=SupportItem.CONSULTATION,
                occurred_on=date(2026, 10, 10),
                start_time=time(14, 0),
                method="phone",
                staff=ctx.staff,
                body="寮のエアコンが故障して動かない",
                languages=[_language("zh-hans")],
            ),
            category="daily_life",
        ),
        by=ctx.user,
        today=date(2026, 10, 10),
    )
    for on, body, status in (
        (
            date(2026, 10, 11),
            "受入れ機関の総務部（田中 一郎）に修理を依頼した。",
            ConsultationStatus.IN_PROGRESS,
        ),
        (date(2026, 10, 13), "修理が終わったことを本人に確認した。", ConsultationStatus.RESOLVED),
    ):
        consultations.add_entry(
            case,
            EntryInput(
                occurred_on=on, method="phone", staff=ctx.staff, body=body, status_after=status
            ),
            by=ctx.user,
            today=on,
        )
    if with_docs:
        _interview_reports(ctx, first)
        _interview_reports(ctx, second)
        starts_on, ends_on = date(2026, 4, 1), date(2027, 3, 31)
        rows = form_data_consultation.consultations_for(ctx.host, starts_on, ends_on)
        with as_of(date(2026, 10, 18)):
            for number, page in enumerate(form_data_consultation.pages(rows), start=1):
                ctx.documents[f"5-4-{number}"] = _generate(
                    "5-4", ctx.host, form_data_consultation.build(starts_on, ends_on, page), ends_on
                )


def _interview_reports(ctx: ScenarioContext, interview: PeriodicInterview) -> None:
    on = interview.basis_on
    with as_of(on, time(17)):
        for form_id in ("5-5", "5-6"):
            ctx.documents[f"{form_id}-{on.isoformat()}"] = _generate(
                form_id,
                interview,
                form_data_interview.build(interview, form_id, made_on=on),
                on,
            )


# --- 実行・消す --------------------------------------------------------------------------


@dataclass
class ScenarioResult:
    until: int
    with_docs: bool
    rows: int
    documents: int
    not_stored: tuple[str, ...] = NOT_STORED
    filled: tuple[str, ...] = FILLED


def _context() -> ScenarioContext:
    user = User.objects.filter(username=DUMMY_USERNAME).first()
    staff = Staff.objects.filter(user=user).first() if user is not None else None
    if user is None or staff is None:
        raise ScenarioError("The staff member dummy_tanto is not found. Run seed_dummy first.")
    return ScenarioContext(staff=staff, user=user)


def conflicts() -> list[str]:
    """台帳に無い（画面で作った）同じ受入れ機関・外国人。あれば作らずに止める（消さない）。"""
    ledger = SeedObject.all_objects.values("object_id")
    found = [
        f"host organization #{host.pk} {host.name} (corporate number {host.corporate_number})"
        for host in HostOrganization.objects.filter(corporate_number="7123456789012").exclude(
            pk__in=ledger.filter(content_type__model="hostorganization")
        )
    ]
    found += [
        f"foreign worker #{worker.pk} {worker.name_romaji} (born {worker.birth_date})"
        for worker in ForeignWorker.objects.filter(
            name_romaji=WORKER_ROMAJI, birth_date=date(2001, 5, 14)
        ).exclude(pk__in=ledger.filter(content_type__model="foreignworker"))
    ]
    return found


@transaction.atomic
def run(until: int = LAST_SECTION, *, with_docs: bool = False) -> ScenarioResult:
    builders.ensure_allowed()
    if not 1 <= until <= LAST_SECTION:
        raise ScenarioError(f"--until must be 1 to {LAST_SECTION}.")
    found = conflicts()
    if found:
        raise ScenarioError(
            "Records of the scenario made on the screens exist (not in the dummy ledger, so they "
            "are not deleted): " + "; ".join(found) + ". Remove or rename them first."
        )
    ctx = _context()
    steps = {
        1: lambda: section_1(ctx),
        2: lambda: section_2(ctx),
        3: lambda: section_3(ctx),
        4: lambda: section_4(ctx),
        5: lambda: section_5(ctx, with_docs),
        6: lambda: section_6(ctx, with_docs),
        7: lambda: section_7(ctx, with_docs),
        8: lambda: section_8(ctx, with_docs),
    }
    for number in range(1, until + 1):
        steps[number]()
    return ScenarioResult(until, with_docs, len(ctx.created), len(ctx.documents))


@transaction.atomic
def reset() -> int:
    """シナリオの行だけを消す（seed_dummy のダミーデータは消さない）。"""
    builders.ensure_allowed()
    return builders.reset(SeedObject.scenario_objects.all(), scope=SeedScope.SCENARIO)


def worker() -> ForeignWorker | None:
    ids = SeedObject.scenario_objects.filter(content_type__model="foreignworker").values(
        "object_id"
    )
    return ForeignWorker.objects.filter(pk__in=ids).first()
