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
- 段階4の台帳の項目（節 1〜3 の一括申請の項目）は `ledger` のときだけ入れる。一括申請（節 6 の
  6-18〜6-20。認定・基準日 2026-07-10）は `bulk` のときだけ節 5 の後に作る（`ledger` を含む。
  Excel は作らない）。既定（どちらも無し）は段階3の本線のまま。
業務のルール（検査・タスクの起票）は services をそのまま呼ぶ。値はシナリオの「本線のダミーデータ
一覧」のとおり。値の無い欄は空にする。作れなかった値は NOT_STORED に書く。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, time
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.files.base import ContentFile
from django.db import models, transaction

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
    Office,
    Organization,
    OrganizationOfficer,
    Staff,
    SupportLanguage,
    User,
)
from apps.forms_engine.models import BulkApplication, BulkProcedure, GeneratedDocument
from apps.hosts.models import (
    CareerPlanLevel,
    CareerPlanVersion,
    FeeTiming,
    HostApplicantContact,
    HostCooperationConfirmation,
    HostDeclaration,
    HostFinancialYear,
    HostMembership,
    HostOfficer,
    HostOrganization,
    HostSite,
    OperationCategory,
    SpecifiedSkillField,
    SupportContract,
)
from apps.seed import builders
from apps.seed.context import DUMMY_USERNAME, as_of, dummy_pdf
from apps.seed.models import SeedObject, SeedScope
from apps.seed.photo_builders import dummy_image
from apps.support.models import (
    ConsultationStatus,
    InterviewPreference,
    PeriodicInterview,
)
from apps.support.models import SupportPlan as SupportPlanOf
from apps.workers.models import (
    ApplicationType,
    ForeignWorker,
    InternshipCompletion,
    PassportRecord,
    QualificationArea,
    ResidenceRecord,
    Sex,
    StatusOfResidence,
    WorkerAddress,
    WorkerContact,
    WorkerPhoto,
    WorkerQualification,
    WorkerRelative,
    WorkerStatus,
    WorkHistory,
)
from services import (
    addresses,
    briefings,
    bulk_application_data,
    bulk_applications,
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
    host_application,
    host_declarations,
    host_records,
    interview_answers,
    support_interviews,
    support_plans,
    support_records,
    wage_explanations,
    worker_documents,
    worker_photos,
    workers,
)
from services import organization as organization_records
from services.attachments import create_attachment
from services.briefings import BriefingInput, CheckInput, FeeInput, SessionInput
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
HOST_NAME = "さくら台フーズ株式会社"
WORKER_ROMAJI = "WANG LINA"
# 業務区分は 2026 年に選べる「飲食料品製造業全般」（「飲食料品製造業」は 2027-04-01 から）
FOOD_CATEGORY = "food_manufacturing_general"
AUTHOR = "代表取締役　高橋 誠"  # 第1-25号・第5-10号・第13-1号の作成責任者
SIGNED_ON = date(2026, 7, 8)  # 節 6 の署名済みの添付の署名日

# 値はあるが、今の作りに欄が無い・決まった形で入れられないもの（報告に書く）
NOT_STORED = (
    "外国人の出生地（中国河南省洛陽市）など段階4の台帳の項目: --ledger（make の LEDGER=1）の"
    "ときだけ入れる",
    "協議会の名称（食品産業特定技能協議会）: 加入の行に協議会の名称の欄が無い（分野で決まる）",
    "報酬の説明の賃金規程の報酬（月給 205,000〜230,000 円）: 欄は 1 つの額なので "
    "205,000 円を入れた",
    "住居の物件名（さくら台寮 203号室）: 住居の概要に物件名の欄が無い",
    "居室の広さ 9.5㎡: 欄は「1人当たり7.5㎡以上」の印なので、印を付けた（1 人で 9.5㎡）",
    "相談・苦情の受付の「日曜・祝日は緊急時のみ」: 対応時間の欄は曜日ごとの時刻なので日曜・祝日は"
    "空にし、緊急時の連絡方法（電話 080-0000-9999）で表した",
)


# 値に無いが、入力が必須なので決めて入れたもの（報告に書く）。2026-10-20 にテストシナリオに
# 同じ値（キャリアアッププランの適用開始日とキャリアルート・支援計画の適用開始日・雇用契約の第1版の
# 変更日・業務区分・本人確認の印・2 回目の面談の ⑥ の対応・費用の内訳）が入ったので、今は無い
FILLED: tuple[str, ...] = ()

# --ledger・--bulk のときの、欄が無い・決まった形で入れられないもの（報告に書く）
LEDGER_NOT_STORED = (
    "自機関の法人番号（8300001234567）: seed_dummy の値があるので変えない",
    "受入れ機関の申請担当者の部署（総務部）: 申請担当者に部署の欄が無い",
    "受入れ機関の決算の年度の始め（2025-04-01）: 決算は期末の日（2026-03-31）だけを持つ。純損益・"
    "純資産は値が無いので空",
    "第1-11-1号 ３ の人数（常勤の職員 120 人・特定技能外国人 0 人）: ３ は離職者数と行方不明者数の"
    "欄。120 人は事業所の常勤職員数に入れた",
    "職歴の仕事の内容（冷凍食品の製造）: 職歴の行に内容の欄が無い（勤務先の名称だけ）",
)
BULK_NOT_STORED = (
    "主たる職種（飲食料品製造業全般）: ツールの選択肢に無いので、契約の業務と事業所の産業分類に"
    "合う「そう菜製造業」にした",
    "外国の機関名と支払額（鄭州サンプル人材服務・150,000 円）: 一括申請の値は事前ガイダンスの記録"
    "（鄭州サンプル人材服務（架空の送出機関）・150000円）から取る（行の入力は使われない）",
)
# --ledger のときの、値に無いが入力が必須なので決めて入れたもの
LEDGER_FILLED = (
    "技能水準・日本語能力の受験地: ツールで必須なので中国にした",
    "受入れ機関の申告: (21) 無・(26) 有・(32) 有（通常の答え）、(27)(28) 空（労働者派遣ではない）、"
    "(29) の内容「労災保険・雇用保険に加入」、申告日 2026-07-10（申請の基準日）",
    "第1-11-1号 ３: 離職者数は 4 つとも 0 人、行方不明者数は特定技能 0 人（技能実習は実習実施者"
    "ではないので空）",
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


# ダミーの PDF に書く書類の名前（英字。統合 PDF で見分けられるように）
PDF_LABELS = {
    "協議会構成員証明書.pdf": "Council membership certificate (host organization)",
    "協力確認書控え_船橋市.pdf": "Cooperation confirmation (Funabashi)",
    "協力確認書控え_市川市.pdf": "Cooperation confirmation (Ichikawa)",
    "支援委託契約書.pdf": "Support outsourcing contract",
    "健康診断個人票.pdf": "Health check record",
}


def _pdf(name: str) -> ContentFile[bytes]:
    label = PDF_LABELS.get(name) or (
        "Signed copy of form " + name.removeprefix("署名済み_").removesuffix(".pdf")
        if name.startswith("署名済み_")
        else name
    )
    return ContentFile(dummy_pdf(label), name=name)


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
        "phone_number": "03-0000-1111",  # 第1-17号Ⅲ４（空のときだけ）
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
        hiring_health_check_on=date(2026, 9, 1),  # 雇入れ時 2026 年 9 月（欄は月）
        first_periodic_health_check_on=date(2027, 9, 1),  # 初回の定期健康診断 2027 年 9 月
        periodic_health_check_interval="1年",
        has_deduction_agreement=True,  # 社宅費・水道光熱費の控除
        work_rules_location="総務部に備え付け（本社工場 事務所）",
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
    for kind, name, amount in (
        ("tax", "", 4500),
        ("social_insurance", "", 31900),
        ("employment_insurance", "", 1300),
        ("housing", "社宅費", 25000),
        ("utilities", "水道光熱費", 8000),
    ):
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
    content.responsible_name = AUTHOR  # 代表取締役 高橋 誠
    content.items[SupportItem.PRE_GUIDANCE] = replace(
        content.items[SupportItem.PRE_GUIDANCE], planned_hours=Decimal(3)
    )
    content.items[SupportItem.ORIENTATION] = replace(
        content.items[SupportItem.ORIENTATION], planned_hours=Decimal(8)
    )
    pickup = content.activities["2.a"]
    content.activities["2.a"] = replace(
        pickup,
        methods=sorted({*pickup.methods, "arrival_airport", "transport"}),
        method_notes={"arrival_airport": "成田国際空港", "transport": "社用車"},
    )
    content.housing = support_plans.HousingContent(
        timing="secured",
        room_area_ok=True,  # 居室 9.5㎡・1 人（欄は「1人当たり7.5㎡以上」の印）
        occupants=1,
        bedroom_area_ok=True,
        rent_collected=True,
        monthly_rent=25000,
        property_type="leased",
        rent_explanation="借上げの賃料（管理費を含む）月 25,000 円を入居者 1 人で除した額",
    )
    content.consultation = support_plans.ConsultationContent(
        hours={
            **{day: ["09:00", "18:00"] for day in ("mon", "tue", "wed", "thu", "fri")},
            "sat": ["09:00", "12:00"],
        },
        methods=["in_person", "phone", "email"],
        phone="03-0000-1111",
        email="soudan@example.com",
        emergency_methods=["phone"],
        emergency_phone="080-0000-9999",
    )
    content.municipality_checks = [
        support_plans.MunicipalityContent(
            place="site",
            prefecture="千葉県",
            municipality="船橋市",
            checked_on=date(2026, 7, 4),
            method="website",
        ),
        support_plans.MunicipalityContent(
            place="residence",
            same_as_site=False,
            prefecture="千葉県",
            municipality="市川市",
            checked_on=date(2026, 7, 4),
            method="website",
        ),
    ]
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
            foreign_fees="paid",
            fees=[
                FeeInput(
                    "鄭州サンプル人材服務（架空の送出機関）",
                    date(2026, 5, 20),
                    150000,
                    "紹介手数料",  # 内訳は値に無いが必須（FILLED）
                )
            ],
            fees_agreed=True,
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


# --- 段階4の台帳の項目（--ledger。節 1〜3 の一括申請の項目） ------------------------------------

BULK_BASIS = date(2026, 7, 10)  # 一括申請（認定）の基準日。申告日も同じ日にする
ORGANIZATION_ADDRESS = ("101-0021", "東京都千代田区外神田一丁目2番3号 サンプルビル4階")
STAFF_EMAIL = "sato.hanako@example.com"
# 申告で「有」にする項目（指示の (23)(29)(30)(31) と、通常の答えが「有」の (26)(32)）
DECLARATION_YES = ("23", "26", "29", "30", "31", "32")
DECLARATION_BLANK = ("27", "28")  # 労働者派遣ではない（ADR-0038 決定 10）
PHOTO_TAKEN_ON = date(2026, 6, 20)
PHOTO_COLOR = (205, 214, 236)


def _replace_dummy_address(place: Organization | Office) -> bool:
    """所在地が空か、ツールの表で分けられない（seed のダミー）なら、シナリオの値にする。"""
    if place.address and addresses.split(place.address).ok:
        return False
    place.postal_code, place.address = ORGANIZATION_ADDRESS
    return True


def ledger_1(ctx: ScenarioContext) -> None:
    """自機関の番号・所在地・役員と、取次者（佐藤 花子）の印とメール。"""
    organization = Organization.objects.get()
    if not organization.corporate_number:
        organization.corporate_number = "8300001234567"
    organization.employment_insurance_number = "13016543210"  # 1301-654321-0
    _replace_dummy_address(organization)
    organization_records.save_record(organization, by=ctx.user)
    office = ctx.staff.office
    if office is not None and _replace_dummy_address(office):
        organization_records.save_record(office, by=ctx.user)
    officer = OrganizationOfficer(
        organization=organization, name="小林 直人", position="代表取締役"
    )
    ctx.keep(host_application.save_row(officer, by=ctx.user))
    ctx.staff.is_online_application_user = True
    ctx.staff.online_application_email = STAFF_EMAIL
    organization_records.save_record(ctx.staff, by=ctx.user)


def _declaration_answers() -> dict[str, dict[str, str]]:
    answers = {}
    for item in host_declarations.items():
        if item.key in DECLARATION_BLANK:
            continue
        detail = "労災保険・雇用保険に加入" if item.key == "29" else ""
        answers[item.key] = {
            "answer": "yes" if item.key in DECLARATION_YES else "no",
            "detail": detail,
        }
    return answers


def ledger_2(ctx: ScenarioContext) -> None:
    """受入れ機関の業種・資本金・決算・申告・役員・申請担当者と、事業所の職員数・番号・保険。"""
    host, site = ctx.host, ctx.site
    assert host is not None and site is not None
    host.industry_1 = bulk_application_data.code_by_japanese("CC13", "食料品製造業", "coe")
    host.capital = 30_000_000
    host_records.save_host(host, by=ctx.user)
    year = HostFinancialYear(
        host_organization=host,
        fiscal_year_end=date(2026, 3, 31),  # 2025 年度（2025-04-01〜2026-03-31）
        sales=1_200_000_000,
        ordinary_profit=36_000_000,
    )
    ctx.keep(host_application.save_row(year, by=ctx.user))
    declaration = HostDeclaration(
        host_organization=host,
        declared_on=BULK_BASIS,
        left_japanese_voluntary=0,
        left_japanese_involuntary=0,
        left_foreign_voluntary=0,
        left_foreign_involuntary=0,
        missing_ssw=0,
    )
    ctx.keep(host_application.add_declaration(declaration, _declaration_answers(), by=ctx.user))
    for name, position, not_involved in (
        ("高橋 誠", "代表取締役", False),
        ("中村 由美", "取締役", True),  # 受入れ業務の執行に直接関与しない（第1-23号）
    ):
        officer = HostOfficer(
            host_organization=host,
            name=name,
            position=position,
            not_involved_in_acceptance=not_involved,
        )
        ctx.keep(host_application.save_row(officer, by=ctx.user))
    contact = HostApplicantContact(
        host_organization=host,
        name="田中 一郎",
        relationship="所属機関の職員",
        postal_code="273-0012",
        address="千葉県船橋市浜町四丁目10番1号",
        phone_number="047-400-1236",
        is_active=True,
    )
    ctx.keep(host_application.save_row(contact, by=ctx.user))
    site.full_time_employees = 120
    site.employment_insurance_number = "12011234567"  # 1201-123456-7
    site.labour_insurance_number = "12101123456000"
    site.health_pension_insured = True
    site.labour_insurance_covered = True
    host_application.save_row(site, by=ctx.user)


def ledger_3(ctx: ScenarioContext) -> None:
    """外国人の出生地・在日親族・職歴・試験（合格証明書つき）・申請用の写真。技能実習2号は無し。"""
    worker = ctx.worker
    assert worker is not None
    worker.birthplace = "中国河南省洛陽市"
    worker.has_relatives_in_japan = False
    worker.has_work_history = True
    workers.update_worker(worker, by=ctx.user, loaded_updated_at=None)
    history = WorkHistory(
        joined_on=date(2019, 7, 1),  # 年月は月の 1 日で持つ
        left_on=date(2025, 12, 1),
        employer="鄭州サンプル食品有限公司",
    )
    ctx.keep(workers.add_record(worker, history, by=ctx.user))
    for area, exam, passed_on, filename in (
        (QualificationArea.SKILL, "017", date(2026, 3, 15), "合格証明書_技能測定試験.pdf"),
        (QualificationArea.JAPANESE, "002", date(2026, 2, 20), "合格証明書_日本語基礎テスト.pdf"),
    ):
        row = WorkerQualification(
            area=area,
            evaluation="03",  # 特定技能評価試験
            # 017 飲食料品製造業特定技能１号技能測定試験・002 国際交流基金日本語基礎テスト
            exam=exam,
            exam_country="039",  # 中国（値に無い。LEDGER_FILLED）
            passed_on=passed_on,
        )
        files = [(AttachmentKind.QUALIFICATION_CERTIFICATE, _pdf(filename))]
        ctx.keep(workers.add_record(worker, row, by=ctx.user, files=files))
        for attachment in Attachment.objects.filter(
            content_type=ContentType.objects.get_for_model(WorkerQualification), object_id=row.pk
        ):
            ctx.keep(attachment)
    photo = worker_photos.register(
        worker, dummy_image(PHOTO_COLOR), taken_on=PHOTO_TAKEN_ON, box=None, by=ctx.user
    )
    ctx.keep(photo)


# --- 一括申請（--bulk。節 6 の 6-18〜6-20。Excel は作らない） ----------------------------------

BULK_INPUTS = {
    "spouse": "no",
    "occupation": "無職",
    # 日本における連絡先（入国前。入居予定の寮。ADR-0039 決定 17）
    "contact_postal": "272-0023",
    "contact_address": "千葉県市川市南八幡五丁目8番8号 さくら台寮203号室",
    "contact_phone": "080-0000-5678",
    "entry_month": "2026年9月",
    "port": "01",  # 成田空港(NRT)
    "stay_years": "1",
    "stay_months": "0",
    "companion": "no",
    "visa_place": "北京",
    "entry_history": "yes",
    "entry_count": "1",
    "last_entry_from": "20240401",
    "last_entry_to": "20240407",
    "coe_history": "no",
    "deport": "no",
    "receipt": "03",  # メール
    "notice_email": STAFF_EMAIL,
    "main_occupation": "L35",  # そう菜製造業（「飲食料品製造業全般」は無い。BULK_NOT_STORED）
    "hours_equal": "yes",
    "pay_equal": "yes",
    "discrimination": "no",
    "home_leave": "yes",
    "return_travel": "yes",
    "health": "yes",
    "plan_appropriate": "yes",
    "deposit": "no",
    "foreign_fee": "yes",
    "foreign_fee_org": "鄭州サンプル人材服務",
    "foreign_fee_amount": "150000円",
}


def section_bulk(ctx: ScenarioContext) -> None:
    """一括申請（認定・2026-07-10・取次者 佐藤 花子）と王さんの行（代理人 田中 一郎）。"""
    assert ctx.worker is not None
    application = BulkApplication(
        procedure=BulkProcedure.COE,
        basis_on=BULK_BASIS,
        intermediary=ctx.staff,
        created_by=ctx.user,
        updated_by=ctx.user,
    )
    application.save()
    ctx.keep(application)
    for entry in bulk_applications.add_workers(application, [ctx.worker], by=ctx.user):
        ctx.keep(entry)
        bulk_applications.save_inputs(
            entry,
            {**(entry.inputs or {}), **BULK_INPUTS},
            sensitive={"crime": "no", "crime_detail": ""},
            by=ctx.user,
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
    ledger: bool = False
    bulk: bool = False


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


# --- --replace: 画面で作った本線と同じ記録を消す（開発用。DEBUG のときだけ） --------------------


def replacement_rows() -> dict[type[models.Model], list[int]]:
    """台帳の外の、本線と同じ名前の受入れ機関・外国人と、それに紐づく行（種類ごとの ID）。

    支援のデータ（計画・記録・面談・相談・交付・生成物・外国人の添付）と届出タスクは、
    builders.delete_rows が外国人・受入れ機関から辿って消す。
    """
    from apps.contracts.models import EmploymentContractShift
    from apps.hosts.models import CareerPlan

    ledger = SeedObject.all_objects.values("object_id")
    hosts = list(
        HostOrganization.objects.filter(
            models.Q(name=HOST_NAME) | models.Q(corporate_number="7123456789012")
        )
        .exclude(pk__in=ledger.filter(content_type__model="hostorganization"))
        .values_list("pk", flat=True)
    )
    people = list(
        ForeignWorker.objects.filter(name_romaji=WORKER_ROMAJI)
        .exclude(pk__in=ledger.filter(content_type__model="foreignworker"))
        .values_list("pk", flat=True)
    )
    contracts_ = EmploymentContract.objects.filter(
        models.Q(worker_id__in=people) | models.Q(host_organization_id__in=hosts)
    )
    others = contracts_.exclude(worker_id__in=people)
    if (
        others.exists()
        or SupportPlanOf.objects.filter(host_organization_id__in=hosts)
        .exclude(worker_id__in=people)
        .exists()
    ):
        raise ScenarioError(
            "Other foreign workers have contracts or support plans with the host organization; "
            "it is not deleted."
        )
    versions = EmploymentContractVersion.objects.filter(contract__in=contracts_)
    plans = CareerPlan.objects.filter(host_organization_id__in=hosts)
    support = SupportContract.objects.filter(host_organization_id__in=hosts)
    memberships = HostMembership.objects.filter(host_organization_id__in=hosts)
    cooperation = HostCooperationConfirmation.objects.filter(host_organization_id__in=hosts)

    def ids(queryset: models.QuerySet[Any]) -> list[int]:
        return list(queryset.values_list("pk", flat=True))

    qualifications = WorkerQualification.objects.filter(worker_id__in=people)
    internships = InternshipCompletion.objects.filter(worker_id__in=people)
    attachment_targets = models.Q(pk__in=[])
    for model, queryset in (
        (WorkerQualification, qualifications),
        (InternshipCompletion, internships),
        (HostOrganization, HostOrganization.objects.filter(pk__in=hosts)),
        (HostMembership, memberships),
        (HostCooperationConfirmation, cooperation),
        (SupportContract, support),
        (EmploymentContract, contracts_),
        (EmploymentContractVersion, versions),
    ):
        attachment_targets |= models.Q(
            content_type=ContentType.objects.get_for_model(model),
            object_id__in=queryset.values("pk"),
        )
    rows: dict[type[models.Model], list[int]] = {
        HostOrganization: hosts,
        HostSite: ids(HostSite.objects.filter(host_organization_id__in=hosts)),
        HostMembership: ids(memberships),
        HostCooperationConfirmation: ids(cooperation),
        SupportContract: ids(support),
        CareerPlan: ids(plans),
        CareerPlanVersion: ids(CareerPlanVersion.objects.filter(plan__in=plans)),
        CareerPlanLevel: ids(CareerPlanLevel.objects.filter(version__plan__in=plans)),
        EmploymentContract: ids(contracts_),
        EmploymentContractVersion: ids(versions),
        EmploymentContractAllowance: ids(
            EmploymentContractAllowance.objects.filter(version__in=versions)
        ),
        EmploymentContractDeduction: ids(
            EmploymentContractDeduction.objects.filter(version__in=versions)
        ),
        EmploymentContractShift: ids(EmploymentContractShift.objects.filter(version__in=versions)),
        ResidenceRecord: ids(ResidenceRecord.objects.filter(worker_id__in=people)),
        PassportRecord: ids(PassportRecord.objects.filter(worker_id__in=people)),
        WorkerAddress: ids(WorkerAddress.objects.filter(worker_id__in=people)),
        WorkerContact: ids(WorkerContact.objects.filter(worker_id__in=people)),
        # 段階4の台帳の項目（一括申請の項目）
        WorkerRelative: ids(WorkerRelative.objects.filter(worker_id__in=people)),
        WorkerQualification: ids(qualifications),
        InternshipCompletion: ids(internships),
        WorkHistory: ids(WorkHistory.objects.filter(worker_id__in=people)),
        HostFinancialYear: ids(HostFinancialYear.objects.filter(host_organization_id__in=hosts)),
        HostDeclaration: ids(HostDeclaration.objects.filter(host_organization_id__in=hosts)),
        HostOfficer: ids(HostOfficer.objects.filter(host_organization_id__in=hosts)),
        HostApplicantContact: ids(
            HostApplicantContact.objects.filter(host_organization_id__in=hosts)
        ),
        ForeignWorker: people,
        Attachment: ids(Attachment.objects.filter(attachment_targets)),
    }
    return {model: values for model, values in rows.items() if values}


def replacement_counts(rows: dict[type[models.Model], list[int]]) -> dict[str, int]:
    """消す行の件数（種類ごと。支援のデータ・生成物・届出タスクは外国人・受入れ機関から数える）。"""
    from apps.contracts.models import EmploymentConditionTemplate
    from apps.filings.models import Filing
    from apps.forms_engine.models import BulkApplicationEntry, SubmissionBundle, SubmissionDraft
    from apps.hosts.models import HostDocument
    from apps.support.models import (
        Briefing,
        Consultation,
        DocumentDelivery,
        SupportPlan,
        SupportRecord,
    )

    people = rows.get(ForeignWorker, [])
    hosts = rows.get(HostOrganization, [])
    counts = {model._meta.label: len(values) for model, values in rows.items()}
    worker_files = Attachment.objects.filter(
        content_type__model__in=["foreignworker", "supportrecord", "documentdelivery"],
        object_id__in=[
            *people,
            *SupportRecord.objects.filter(worker_id__in=people).values_list("pk", flat=True),
            *DocumentDelivery.objects.filter(worker_id__in=people).values_list("pk", flat=True),
        ],
    )
    filings = models.Q(worker_id__in=people)
    for model, values in rows.items():
        filings |= models.Q(
            content_type=ContentType.objects.get_for_model(model), object_id__in=values
        )
    extra = {
        "support.SupportPlan": SupportPlan.objects.filter(worker_id__in=people).count(),
        "support.SupportRecord": SupportRecord.objects.filter(worker_id__in=people).count(),
        "support.PeriodicInterview": PeriodicInterview.objects.filter(worker_id__in=people).count(),
        "support.Consultation": Consultation.objects.filter(worker_id__in=people).count(),
        "support.Briefing": Briefing.objects.filter(worker_id__in=people).count(),
        "support.DocumentDelivery": DocumentDelivery.objects.filter(worker_id__in=people).count(),
        "forms_engine.GeneratedDocument": GeneratedDocument.objects.filter(
            models.Q(worker_id__in=people)
            | models.Q(content_type__model="hostorganization", object_id__in=hosts)
        ).count(),
        "core.Attachment (worker and support)": worker_files.count(),
        "hosts.HostDocument": HostDocument.objects.filter(
            models.Q(host_organization_id__in=hosts) | models.Q(site_id__in=rows.get(HostSite, []))
        ).count(),
        "forms_engine.SubmissionBundle": SubmissionBundle.objects.filter(
            worker_id__in=people
        ).count(),
        "forms_engine.SubmissionDraft": SubmissionDraft.objects.filter(
            worker_id__in=people
        ).count(),
        "contracts.EmploymentConditionTemplate": EmploymentConditionTemplate.objects.filter(
            host_organization_id__in=hosts
        ).count(),
        "core.Attachment (host documents and submission drafts)": Attachment.objects.filter(
            models.Q(
                content_type=ContentType.objects.get_for_model(HostDocument),
                object_id__in=HostDocument.objects.filter(
                    models.Q(host_organization_id__in=hosts)
                    | models.Q(site_id__in=rows.get(HostSite, []))
                ).values("pk"),
            )
            | models.Q(
                content_type=ContentType.objects.get_for_model(SubmissionDraft),
                object_id__in=SubmissionDraft.objects.filter(worker_id__in=people).values("pk"),
            )
        ).count(),
        "filings.Filing": Filing.objects.filter(filings).count(),
        "workers.WorkerPhoto": WorkerPhoto.objects.filter(worker_id__in=people).count(),
        "forms_engine.BulkApplicationEntry": BulkApplicationEntry.objects.filter(
            worker_id__in=people
        ).count(),
    }
    return counts | {label: count for label, count in extra.items() if count}


def replace_existing(rows: dict[type[models.Model], list[int]]) -> int:
    """画面で作った本線と同じ記録を消す（開発用。DEBUG のときだけ。監査ログは消さない）。"""
    if not settings.DEBUG:
        raise ScenarioError("--replace is only for development (DEBUG=True).")
    builders.ensure_allowed()
    return builders.delete_rows(rows)


@transaction.atomic
def run(
    until: int = LAST_SECTION, *, with_docs: bool = False, ledger: bool = False, bulk: bool = False
) -> ScenarioResult:
    """節 1〜until の記録を作る。ledger で段階4の台帳の項目、bulk で一括申請も（ledger を含む）。"""
    builders.ensure_allowed()
    if not 1 <= until <= LAST_SECTION:
        raise ScenarioError(f"--until must be 1 to {LAST_SECTION}.")
    if bulk and until < 5:
        raise ScenarioError("--bulk needs sections 1 to 5 (--until 5 or more).")
    ledger = ledger or bulk
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
    ledger_steps = {1: ledger_1, 2: ledger_2, 3: ledger_3}
    for number in range(1, until + 1):
        steps[number]()
        if ledger and number in ledger_steps:
            ledger_steps[number](ctx)
        if bulk and number == 5:
            section_bulk(ctx)  # 雇用契約・支援計画・事前ガイダンスの後（6-18〜6-20）
    not_stored: tuple[str, ...] = NOT_STORED
    if ledger:
        not_stored = tuple(line for line in NOT_STORED if "段階4の台帳の項目" not in line)
        not_stored += LEDGER_NOT_STORED + (BULK_NOT_STORED if bulk else ())
    return ScenarioResult(
        until,
        with_docs,
        len(ctx.created),
        len(ctx.documents),
        not_stored=not_stored,
        filled=FILLED + (LEDGER_FILLED if ledger else ()),
        ledger=ledger,
        bulk=bulk,
    )


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
