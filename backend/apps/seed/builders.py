"""ダミーデータの作成（seed_dummy の本体）。開発用。本物の外国人情報は使わない（制約10）。

- 乱数の種は固定。日付は基準日からの相対なので、同じ基準日なら同じ内容になる。
- 期限の分布・履歴・契約のパターンは WORKER_SPECS に明示し、乱数は氏名・番号・生年月日にだけ使う。
- 業務のルール（番号の形式・契約の版・終了・届出の起票）は services をそのまま呼ぶ。
- 段階ごとの関数を STEPS に並べる。段階2で面談記録（seed_support_records）を足す。
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, time, timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.files.base import ContentFile
from django.db import models, transaction

from apps.contracts.models import (
    ContractWageExplanation,
    EmploymentConditionTemplate,
    EmploymentContract,
    EmploymentContractAllowance,
    EmploymentContractDeduction,
    EmploymentContractShift,
    EmploymentContractVersion,
    TerminationReason,
    WageComparison,
    WageUnit,
)
from apps.core.models import (
    Attachment,
    AttachmentKind,
    Country,
    EmploymentType,
    Office,
    OfficeManagerPeriod,
    Organization,
    Role,
    Staff,
    SupportLanguage,
    User,
    UserRole,
)
from apps.filings.models import Filing, FilingStatus
from apps.forms_engine.models import BulkApplicationEntry, BulkFilingRow
from apps.hosts.models import (
    CareerPlan,
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
from apps.seed import bulk_builders, photo_builders, support_builders
from apps.seed.context import (
    DUMMY_MARK,
    DUMMY_USERNAME,
    DUMMY_USERNAME_2,
    SEED,
    SeedContext,
    dummy_pdf,
)
from apps.seed.models import SeedObject, SeedScope
from apps.workers.models import (
    ApplicationType,
    ForeignWorker,
    PassportRecord,
    ResidenceRecord,
    Sex,
    StaffAssignment,
    StatusOfResidence,
    WorkerAddress,
    WorkerContact,
    WorkerPhoto,
    WorkerQualification,
    WorkerStatus,
)
from services import (
    career_plans,
    condition_templates,
    contracts,
    corporate_number,
    deadlines,
    filings,
    host_records,
    passport,
    residence_card,
    support_plans,
)
from services.attachments import create_attachment


class SeedNotAllowedError(Exception):
    pass


def ensure_allowed() -> None:
    """本番で誤って実行しない（DEBUG かつ ALLOW_DUMMY_DATA=true のときだけ）。"""
    if not settings.DEBUG:
        raise SeedNotAllowedError("DEBUG is False. seed_dummy runs only in development.")
    if not getattr(settings, "ALLOW_DUMMY_DATA", False):
        raise SeedNotAllowedError(
            "ALLOW_DUMMY_DATA is not true. Set it in .env (development only)."
        )


# --- 自機関・担当者 ----------------------------------------------------------------


def seed_organization(ctx: SeedContext) -> None:
    """自機関が未登録のときだけ作る（登録済みの自機関には触らない）。"""
    existing = Organization.objects.first()
    if existing is not None:
        ctx.organization = existing
        return
    organization = Organization.objects.create(
        name=f"{DUMMY_MARK}サンプル登録支援機関",
        name_kana="さんぷるとうろくしえんきかん",
        registration_number="99登-999999",
        # 第1-17号Ⅲ欄２に書く「支援業務を開始する予定年月日」（T31）
        support_start_planned_on=ctx.days(-1500),
        # 5 か月後に満了 → 登録更新の受付（6 か月前の月の初日）がすでに始まっている
        registration_expires_on=deadlines.add_months(ctx.base, 5),
        postal_code="000-0001",
        address="東京都サンプル区ダミー町1-1-1",
        phone_number="000-0000-9000",  # 第1-17号Ⅲ４ の電話番号
        food_council_joined_on=ctx.days(-700),
        # 第13-2号の作成責任者・第5-10号の乙の記名と第8条の管轄裁判所（T37）
        representative_title="代表理事",
        representative_name=f"代表 花子{DUMMY_MARK}",
        court_name="東京",
    )
    ctx.keep(organization)
    ctx.keep(
        create_attachment(
            target=organization,
            kind=AttachmentKind.FOOD_COUNCIL_CERTIFICATE,
            file=ContentFile(
                dummy_pdf("Council membership certificate (registered support organization)"),
                name="dummy_food_council.pdf",
            ),
            created_by=None,
            valid_until=ctx.days(200),
        )
    )
    ctx.organization = organization


def _login_user(username: str, last_name: str, first_name: str) -> User:
    user, created = User.objects.get_or_create(
        username=username,
        defaults={
            "last_name": last_name,
            "first_name": first_name,
            "email": f"{username}@example.invalid",
        },
    )
    if created:
        user.set_unusable_password()
        user.save(update_fields=["password"])
    UserRole.objects.get_or_create(user=user, role=Role.objects.get(code="support_staff"))
    return user


def seed_staff(ctx: SeedContext) -> None:
    office = Office.objects.create(
        name=f"本社{DUMMY_MARK}", postal_code="000-0001", address="東京都サンプル区ダミー町1-1-1"
    )
    ctx.keep(office)
    # ログイン用のユーザーは --reset でも消さない
    # （パスワードは設定しない。changepassword かパスワードの再設定〔T25〕で設定する）
    login_user = _login_user(DUMMY_USERNAME, "佐藤", "花子")
    login_user_2 = _login_user(DUMMY_USERNAME_2, "鈴木", "次郎")
    # 役職は第1-17号の「氏名（役職）」・Ⅲ欄７の役職に出る（修正 1 回目 A6 の確かめ）
    for name, kana, position, manager, user in (
        (f"山田 一郎{DUMMY_MARK}", "ヤマダ イチロウ", "所長", True, None),
        (f"佐藤 花子{DUMMY_MARK}", "サトウ ハナコ", "主任", False, login_user),
        (f"鈴木 次郎{DUMMY_MARK}", "スズキ ジロウ", "支援担当", False, login_user_2),
    ):
        staff = Staff.objects.create(
            name=name,
            name_kana=kana,
            position=position,
            is_support_manager=manager,
            is_support_staff=True,
            employment_type=EmploymentType.FULL_TIME,
            office=office,
            training_completed_on=ctx.days(-400),
            has_life_consultation_experience=True,
            life_consultation_experience_note="外国人の生活相談窓口（ダミー）",
            user=user,
        )
        staff.languages.set(SupportLanguage.objects.filter(code__in=["ja", "zh-hans"]))
        ctx.keep(staff)
        ctx.staff.append(staff)
        if manager:
            office.support_manager = staff  # 第1-17号 Ⅲ欄７（修正 1 回目 A5）
            office.save(update_fields=["support_manager", "updated_at"])
            # 支援責任者の履歴（T45）。ダミーは 2 年前から
            OfficeManagerPeriod.objects.create(office=office, staff=staff, starts_on=ctx.days(-730))


# --- 受入れ機関 ------------------------------------------------------------------


def _categories(*codes: str) -> list[OperationCategory]:
    return list(OperationCategory.objects.filter(code__in=codes))


def seed_hosts(ctx: SeedContext) -> None:
    industrial = SpecifiedSkillField.objects.get(code="industrial_products")
    food = SpecifiedSkillField.objects.get(code="food_manufacturing")
    plans: list[dict[str, Any]] = [
        {
            "name": f"{DUMMY_MARK}サンプル精機工業株式会社",
            "kana": "サンプルセイキコウギョウ",
            "field": industrial,
            "sites": [
                (
                    "本社工場",
                    "愛知県サンプル市ダミー町1-2-3",
                    "2699",
                    ["machine_metal_processing", "electrical_electronic_assembly"],
                ),
                ("第二工場", "愛知県サンプル市ダミー町4-5-6", "2814", ["metal_surface_treatment"]),
            ],
            "membership_valid_until": None,
            "municipalities": ["サンプル市"],
            "support_contract": (ctx.days(-730), ctx.days(60)),  # 3 か月以内に終了 → 更新タスク
        },
        {
            "name": f"{DUMMY_MARK}サンプル食品株式会社",
            "kana": "サンプルショクヒン",
            "field": food,
            "sites": [
                (
                    "食品工場",
                    "静岡県ダミー市サンプル町7-8-9",
                    "0999",
                    ["food_manufacturing_general"],
                )
            ],
            "membership_valid_until": ctx.days(25),  # 1 か月以内に期限 → 更新タスク
            "municipalities": ["ダミー市", "サンプル町"],
            "support_contract": (ctx.days(-365), None),
        },
    ]
    for plan in plans:
        host = HostOrganization.objects.create(
            name=plan["name"],
            name_kana=plan["kana"],
            corporate_number=corporate_number.generate_dummy(ctx.rng),
            representative_title="代表取締役",
            representative_name=f"代表 太郎{DUMMY_MARK}",
            postal_code="000-0002",
            address=plan["sites"][0][1],
            phone_number="000-0000-0000",
        )
        ctx.keep(host)
        ctx.hosts.append(host)
        host_sites = []
        for site_name, address, industry_code, category_codes in plan["sites"]:
            site = HostSite.objects.create(
                host_organization=host,
                name=site_name,
                postal_code="000-0002",
                address=address,
                phone_number=f"000-0000-1{len(ctx.sites) + 1:03d}",
                industry_code=industry_code,
                field=plan["field"],
            )
            site.categories.set(_categories(*category_codes))
            ctx.keep(site)
            ctx.sites.append(site)
            host_sites.append(site)
        membership = HostMembership.objects.create(
            host_organization=host,
            field=plan["field"],
            joined_on=ctx.days(-800),
            valid_until=plan["membership_valid_until"],
            member_number=f"DUMMY-{len(ctx.hosts):04d}",
        )
        ctx.keep(membership)
        ctx.keep(
            create_attachment(
                target=membership,
                kind=AttachmentKind.MEMBERSHIP_CERTIFICATE,
                file=ContentFile(
                    dummy_pdf("Council membership certificate (host organization)"),
                    name="dummy_membership.pdf",
                ),
                created_by=None,
                valid_until=plan["membership_valid_until"],
            )
        )
        # 協力確認書は最初の事業所（就業の場所）の行で、所在地と住居地の市区町村へ（ADR-0015）
        for municipality in plan["municipalities"]:
            ctx.keep(
                HostCooperationConfirmation.objects.create(
                    host_organization=host,
                    site=host_sites[0],
                    municipality=municipality,
                    submitted_on=ctx.days(-600),
                )
            )
        starts_on, ends_on = plan["support_contract"]
        support = SupportContract.objects.create(
            host_organization=host,
            monthly_fee=30000,  # 委託料（1 名あたり月額。ADR-0017）
            concluded_on=starts_on - timedelta(days=14),
            starts_on=starts_on,
            ends_on=ends_on,
        )
        ctx.keep(support)
        seed_fees(support)


def seed_fees(support: SupportContract) -> None:
    """委託料の内訳（第5-10号の別紙。合計は月額 30,000 円。T37-D9）。内訳は契約とともに消える。"""
    host_records.save_fees(
        support,
        [
            host_records.FeeInput(
                name="義務的支援の委託料", amount=25000, timing=FeeTiming.REGULAR
            ),
            host_records.FeeInput(
                name="任意的支援（通訳）の委託料", amount=5000, timing=FeeTiming.REGULAR
            ),
        ],
        by=None,
    )


# --- 外国人 ----------------------------------------------------------------------

# (漢字, 簡体字, ピンイン)
CN_SURNAMES = [
    ("王", "王", "WANG"),
    ("李", "李", "LI"),
    ("張", "张", "ZHANG"),
    ("劉", "刘", "LIU"),
    ("陳", "陈", "CHEN"),
    ("楊", "杨", "YANG"),
    ("趙", "赵", "ZHAO"),
    ("黄", "黄", "HUANG"),
    ("周", "周", "ZHOU"),
    ("呉", "吴", "WU"),
    ("徐", "徐", "XU"),
    ("孫", "孙", "SUN"),
    ("馬", "马", "MA"),
    ("朱", "朱", "ZHU"),
    ("胡", "胡", "HU"),
]
CN_GIVEN = [
    ("芳", "芳", "FANG"),
    ("静", "静", "JING"),
    ("偉", "伟", "WEI"),
    ("敏", "敏", "MIN"),
    ("磊", "磊", "LEI"),
    ("洋", "洋", "YANG"),
    ("艶", "艳", "YAN"),
    ("勇", "勇", "YONG"),
    ("軍", "军", "JUN"),
    ("傑", "杰", "JIE"),
    ("婷", "婷", "TING"),
    ("雪", "雪", "XUE"),
    ("琳", "琳", "LIN"),
    ("浩", "浩", "HAO"),
    ("宇", "宇", "YU"),
    ("欣", "欣", "XIN"),
    ("涛", "涛", "TAO"),
    ("鵬", "鹏", "PENG"),
    ("麗", "丽", "LI"),
    ("華", "华", "HUA"),
]
VN_SURNAMES = [("Nguyễn", "NGUYEN"), ("Trần", "TRAN"), ("Lê", "LE"), ("Phạm", "PHAM")]
VN_GIVEN = [("An", "AN"), ("Bình", "BINH"), ("Hoa", "HOA"), ("Lan", "LAN"), ("Minh", "MINH")]
ID_NAMES = [("Dewi", "Lestari"), ("Budi", "Santoso"), ("Putri", "Rahma"), ("Agus", "Wijaya")]


@dataclass(frozen=True)
class WorkerSpec:
    """1 人分のパターン。日数は基準日からの相対。"""

    nationality: str  # CN / VN / ID
    residence_expires: int | None  # None = 入国前（在留資格認定証明書の段階）
    history: str = ""  # "" / change / change_extension / reissue
    passport_expires: int = 1800
    contract: str = "plain"  # plain / raise / decrease / voluntary / company / expiring
    wage: str = "monthly"  # monthly / hourly


# 在留期限: 超過 1、7 日以内 1、14 日以内 1、3 か月以内 3（以上で 3 か月以内 5 人＋超過 1 人）、
# 6 か月以内 6、1 年以上 17、入国前 1。旅券の有効期限が 6 か月以内 3 人。
WORKER_SPECS: tuple[WorkerSpec, ...] = (
    WorkerSpec("CN", -5, history="change", contract="decrease"),
    WorkerSpec("CN", 5, passport_expires=60),
    WorkerSpec("CN", 12, history="change_extension", contract="raise"),
    WorkerSpec("CN", 30, contract="voluntary"),
    WorkerSpec("CN", 55, history="reissue"),
    WorkerSpec("CN", 80, contract="company"),
    WorkerSpec("CN", 100, passport_expires=120),
    WorkerSpec("CN", 120, contract="raise"),
    WorkerSpec("CN", 140, history="change"),
    WorkerSpec("CN", 155, contract="expiring"),
    WorkerSpec("CN", 165, wage="hourly"),
    WorkerSpec("CN", 175, passport_expires=170),
    WorkerSpec("CN", 400, history="change", contract="raise"),
    WorkerSpec("CN", 430),
    WorkerSpec("CN", 460, contract="decrease"),
    WorkerSpec("CN", 500, wage="hourly"),
    WorkerSpec("CN", 540),
    WorkerSpec("CN", 580, history="change"),
    WorkerSpec("CN", 620),
    WorkerSpec("CN", 660, wage="hourly"),
    WorkerSpec("CN", 700),
    WorkerSpec("CN", 740),
    WorkerSpec("CN", 780),
    WorkerSpec("CN", 820),
    WorkerSpec("CN", None),  # 入国前
    WorkerSpec("VN", 860),
    WorkerSpec("VN", 900, wage="hourly"),
    WorkerSpec("VN", 960),
    WorkerSpec("ID", 1000),
    WorkerSpec("ID", 1050),
)


# 生成の後で氏名を上書きする人（番号 → 性別 → (漢字, 母国語の表記, ローマ字)）。
# 30 番は乱数では 29 番と同じ氏名（PUTRI RAHMA）になるため（T26）。
# 乱数は消費したうえで上書きし、ほかの人の氏名を変えない
RENAMED: dict[int, dict[str, tuple[str, str, str]]] = {
    30: {
        Sex.FEMALE: ("", "Sari Kurniawati", "SARI KURNIAWATI"),
        Sex.MALE: ("", "Rizky Pratama", "RIZKY PRATAMA"),
    },
}


def _cn_name(ctx: SeedContext, used: set[str]) -> tuple[str, str, str]:
    while True:
        surname = ctx.rng.choice(CN_SURNAMES)
        given = ctx.rng.sample(CN_GIVEN, ctx.rng.choice([1, 2]))
        kanji = surname[0] + "".join(g[0] for g in given)
        if kanji in used:
            continue
        used.add(kanji)
        native = surname[1] + "".join(g[1] for g in given)
        romaji = f"{surname[2]} {''.join(g[2] for g in given)}"
        return kanji, native, romaji


def _vn_name(ctx: SeedContext, sex: str) -> tuple[str, str, str]:
    surname = ctx.rng.choice(VN_SURNAMES)
    middle = ("Văn", "VAN") if sex == Sex.MALE else ("Thị", "THI")
    given = ctx.rng.choice(VN_GIVEN)
    native = f"{surname[0]} {middle[0]} {given[0]}"
    return "", native, f"{surname[1]} {middle[1]} {given[1]}"


def _id_name(ctx: SeedContext) -> tuple[str, str, str]:
    given, family = ctx.rng.choice(ID_NAMES)
    native = f"{given} {family}"
    return "", native, native.upper()


def _staff_period(ctx: SeedContext, worker: ForeignWorker) -> None:
    """担当者の履歴（T45）。ダミーは 2 年前から今の担当者（外国人を消すと一緒に消える）。"""
    StaffAssignment.objects.create(
        worker=worker, staff=worker.assigned_staff, starts_on=ctx.days(-730)
    )


def seed_workers(ctx: SeedContext) -> None:
    countries = {c.code: c for c in Country.objects.filter(code__in=["CN", "VN", "ID"])}
    languages = {lang.code: lang for lang in SupportLanguage.objects.all()}
    support_staff = [s for s in ctx.staff if not s.is_support_manager] or ctx.staff
    used: set[str] = set()
    for index, spec in enumerate(WORKER_SPECS):
        sex = ctx.rng.choice([Sex.MALE, Sex.FEMALE])
        if spec.nationality == "CN":
            kanji, native, romaji = _cn_name(ctx, used)
        elif spec.nationality == "VN":
            kanji, native, romaji = _vn_name(ctx, sex)
        else:
            kanji, native, romaji = _id_name(ctx)
        if index + 1 in RENAMED:
            # 乱数で作った氏名がほかの人と重なるので、乱数の並びは変えずに固定の別名にする
            kanji, native, romaji = RENAMED[index + 1][sex]
        worker = ForeignWorker.objects.create(
            name_romaji=romaji,
            name_kanji=kanji,
            name_native=native,
            nationality=countries[spec.nationality],
            birth_date=ctx.base
            - timedelta(days=365 * ctx.rng.randint(20, 39) + ctx.rng.randint(0, 364)),
            sex=sex,
            support_language=languages["zh-hans" if spec.nationality == "CN" else "ja"],
            assigned_staff=support_staff[index % len(support_staff)],
            status=WorkerStatus.PRE_ENTRY
            if spec.residence_expires is None
            else WorkerStatus.RESIDING,
        )
        ctx.keep(worker)
        ctx.workers.append(worker)
        _staff_period(ctx, worker)
        _residence_history(ctx, worker, spec)
        ctx.keep(
            PassportRecord.objects.create(
                worker=worker,
                number=passport.generate_dummy(ctx.rng),
                issuing_country=countries[spec.nationality],
                issued_on=ctx.days(spec.passport_expires - 3650),
                expires_on=ctx.days(spec.passport_expires),
            )
        )
        ctx.keep(
            WorkerAddress.objects.create(
                worker=worker,
                postal_code="000-0003",
                address=f"愛知県サンプル市ダミー町{index + 1}-1 サンプル寮{index + 101}号",
                effective_from=ctx.days(-300),
            )
        )
        ctx.keep(
            WorkerContact.objects.create(
                worker=worker,
                phone=f"000-0000-{index + 1:04d}",
                email=f"worker{index + 1:02d}@example.invalid",
                emergency_name=f"家族 {index + 1}{DUMMY_MARK}",
                emergency_relationship="母",
                emergency_phone=f"000-0001-{index + 1:04d}",
                home_country_address=f"{DUMMY_MARK}Sample City {index + 1}",
                home_country_phone=f"000-0002-{index + 1:04d}",
                effective_from=ctx.days(-300),
            )
        )


def _residence(ctx: SeedContext, worker: ForeignWorker, **values: Any) -> None:
    ctx.keep(ResidenceRecord.objects.create(worker=worker, **values))


def _residence_history(ctx: SeedContext, worker: ForeignWorker, spec: WorkerSpec) -> None:
    if spec.residence_expires is None:
        _residence(
            ctx,
            worker,
            card_number="",
            status_of_residence=StatusOfResidence.SSW_1,
            application_type=ApplicationType.CERTIFICATE_OF_ELIGIBILITY,
            permitted_on=ctx.days(-20),
            expires_on=ctx.days(70),  # 在留資格認定証明書の有効期間（3 か月）
        )
        return
    expires = ctx.days(spec.residence_expires)
    current_card = residence_card.generate_dummy(ctx.rng)
    if spec.history in ("change", "change_extension"):
        _residence(
            ctx,
            worker,
            card_number=residence_card.generate_dummy(ctx.rng),
            status_of_residence=StatusOfResidence.TECHNICAL_INTERN_3,
            application_type=ApplicationType.EXTENSION,
            permitted_on=expires - timedelta(days=365 * 3),
            expires_on=expires - timedelta(days=365 * 2),
        )
    if spec.history == "change_extension":
        _residence(
            ctx,
            worker,
            card_number=residence_card.generate_dummy(ctx.rng),
            status_of_residence=StatusOfResidence.SSW_1,
            application_type=ApplicationType.CHANGE,
            permitted_on=expires - timedelta(days=365 * 2),
            expires_on=expires - timedelta(days=365),
        )
    application = {
        "change": ApplicationType.CHANGE,
        "change_extension": ApplicationType.EXTENSION,
    }.get(spec.history, ApplicationType.EXTENSION)
    permitted_on = expires - timedelta(days=365)
    _residence(
        ctx,
        worker,
        card_number=current_card,
        status_of_residence=StatusOfResidence.SSW_1,
        application_type=application,
        permitted_on=permitted_on,
        expires_on=expires,
    )
    if spec.history == "reissue":
        # 紛失による再交付（番号が変わり、在留期限は同じ）
        _residence(
            ctx,
            worker,
            card_number=residence_card.generate_dummy(ctx.rng),
            status_of_residence=StatusOfResidence.SSW_1,
            application_type=ApplicationType.CARD_REISSUE,
            permitted_on=permitted_on + timedelta(days=90),
            expires_on=expires,
        )


# --- 雇用契約 ------------------------------------------------------------------------


def _version_values(
    ctx: SeedContext, site: HostSite, spec: WorkerSpec, starts_on: date
) -> dict[str, Any]:
    hourly = spec.wage == "hourly"
    base_wage = 1300 if hourly else 220000 + ctx.rng.randint(0, 6) * 5000
    ends_on = ctx.days(20) if spec.contract == "expiring" else starts_on + timedelta(days=365 * 3)
    return {
        "changed_on": starts_on,
        "signed_on": starts_on - timedelta(days=30),
        "starts_on": starts_on,
        "ends_on": ends_on,
        "renewal": "possible",
        "renewal_criteria": ["workload", "performance", "ability"],
        "site": site,
        "duties": "製造工程の作業（ダミー）",
        "start_time": time(8, 30),
        "end_time": time(17, 30),
        "daily_working_minutes": 480,
        "break_minutes": 60,
        "weekly_working_minutes": 2400,
        # 月の所定労働時間数は年間の 1 か月平均（117600 ÷ 12。労基則 第19条第1項第4号。ADR-0023）
        "monthly_working_minutes": 9800,
        "annual_working_minutes": 117600,
        "weekly_working_days": 5,
        "annual_working_days": 245,
        "regular_holiday_weekdays": ["sat", "sun"],
        "annual_holidays": 120,
        "paid_leave_days": 10,
        "wage_type": "hourly" if hourly else "monthly",
        "base_wage": base_wage,
        # 換算額は第19条の計算（月給 ÷ 月の所定労働時間数。時間給 × 月の所定労働時間数）
        "monthly_equivalent": round(base_wage * 9800 / 60) if hourly else None,
        "hourly_equivalent": None if hourly else round(base_wage * 60 / 9800),
        "overtime_rate_within_60h": 25,
        "overtime_rate_over_60h": 50,
        "holiday_rate_statutory": 35,
        "night_rate": 25,
        "leave_allowance_rate": 60,
        "wage_closing_day": "毎月末日",
        "wage_payment_day": "翌月25日",
        "payment_method": "bank_transfer",
        "has_deduction_agreement": True,
        # 第1-6号の「就業規則を確認できる場所や方法」（空だと生成前の検査で警告。2026-10-20）
        "work_rules_location": "事務室に掲示",
        "has_raise": True,
        "raise_note": "年1回（4月）",
        "has_bonus": True,
        "bonus_note": "年2回（業績による）",
        "resignation_notice_days": 30,
        # 雇用条件書の「詳細は、就業規則 第 条～第 条」（ADR-0016）
        "work_rules_articles_hours": "第15条～第18条（ダミー）",
        "work_rules_articles_holidays": "第19条～第20条（ダミー）",
        "work_rules_articles_leave": "第21条～第24条（ダミー）",
        "work_rules_articles_resignation": "第40条～第42条（ダミー）",
        "insurances": ["employees_pension", "health", "employment", "workers_compensation"],
        "periodic_health_check_interval": "1年ごと",
        # 窓口（Ⅸ４・５）は部署名・担当者職氏名・連絡先の 3 つの欄（T34-D4）
        "treatment_contact_department": "総務部（ダミー）",
        "treatment_contact_person": "総務課長 窓口 一郎（ダミー）",
        "treatment_contact_phone": "000-0000-0100",
        "consultation_contact_department": "総務部（ダミー）",
        "consultation_contact_person": "総務課長 窓口 一郎（ダミー）",
        "consultation_contact_phone": "000-0000-0100",
        "comparable_japanese_note": "同職位の日本人従業員（入社3年目、ダミー）",
        "comparable_japanese_wage": 215000,
    }


def _wage(values: dict[str, Any], base_wage: int) -> dict[str, Any]:
    """基本賃金を変えた版の値（換算額も第19条の計算で作り直す。ADR-0023）。"""
    minutes = values["monthly_working_minutes"]
    if values["wage_type"] == "hourly":
        return {"base_wage": base_wage, "monthly_equivalent": round(base_wage * minutes / 60)}
    return {"base_wage": base_wage, "hourly_equivalent": round(base_wage * 60 / minutes)}


def _wage_explanation(version: EmploymentContractVersion) -> ContractWageExplanation:
    """報酬の説明（第1-4号の元データ。比較対象の日本人あり。日本人の報酬は申請人を下回る）。"""
    hourly = version.wage_type == "hourly"
    return ContractWageExplanation.objects.create(
        version=version,
        comparison=WageComparison.COMPARABLE,
        wage_unit=WageUnit.HOURLY if hourly else WageUnit.MONTHLY,
        applicant_duties="製造ラインの作業員（班長の指示の下で作業。ダミー）",
        applicant_experience_years=Decimal("2"),
        japanese_duties="製造ラインの作業員（入社3年目。班長の指示の下で作業。ダミー）",
        japanese_age=24,
        japanese_sex="male",
        japanese_experience_years=Decimal("3"),
        japanese_wage=1200 if hourly else 205000,
        has_wage_rules=True,
        rules_wage=1150 if hourly else 200000,
        reason="賃金規程の同じ等級の日本人と同じ基準で決めている（ダミー）",
    )


def _add_version(
    ctx: SeedContext,
    values: dict[str, Any],
    *,
    contract: EmploymentContract | None,
    worker: ForeignWorker | None = None,
    allowances: list[tuple[str, int]],
    deductions: list[tuple[str, int]],
) -> EmploymentContractVersion:
    site: HostSite = values["site"]
    version = EmploymentContractVersion(**values)
    contracts.add_version(
        version, contract=contract, worker=worker, host_organization=site.host_organization, by=None
    )
    if contract is None:
        ctx.keep(version.contract)
    ctx.keep(version)
    version.categories.set(list(site.categories.all())[:1])
    for name, amount in allowances:
        ctx.keep(
            EmploymentContractAllowance.objects.create(version=version, name=name, amount=amount)
        )
    for kind, amount in deductions:
        ctx.keep(
            EmploymentContractDeduction.objects.create(version=version, kind=kind, amount=amount)
        )
    ctx.keep(_wage_explanation(version))
    if values["wage_type"] == "hourly":
        ctx.keep(
            EmploymentContractShift.objects.create(
                version=version,
                start_time=time(13, 0),
                end_time=time(22, 0),
                applies_to="遅番の週",
                daily_working_minutes=480,
            )
        )
    contracts.complete_version(version, by=None)  # 変更届出・新たな契約締結の届出の起票（T11）
    return version


def seed_contracts(ctx: SeedContext) -> None:
    allowances = [("住宅手当", 10000), ("皆勤手当", 5000)]
    deductions = [("housing", 30000), ("food", 15000)]
    for index, (worker, spec) in enumerate(zip(ctx.workers, WORKER_SPECS, strict=True)):
        site = ctx.sites[index % len(ctx.sites)]
        starts_on = (
            ctx.days(-ctx.rng.randint(120, 900))
            if spec.residence_expires is not None
            else ctx.days(30)
        )
        values = _version_values(ctx, site, spec, starts_on)
        first = _add_version(
            ctx, values, contract=None, worker=worker, allowances=allowances, deductions=deductions
        )
        contract = first.contract
        if spec.contract == "raise":
            _add_version(
                ctx,
                values
                | _wage(values, values["base_wage"] + 10000)
                | {
                    "changed_on": ctx.days(-30),
                    "change_reason": "昇給（ダミー）",
                },
                contract=contract,
                allowances=allowances,
                deductions=deductions,
            )
        elif spec.contract == "decrease":
            _add_version(
                ctx,
                values
                | _wage(values, values["base_wage"] - 10000)
                | {
                    "changed_on": ctx.days(-3),
                    "change_reason": "減給（ダミー）",
                },
                contract=contract,
                allowances=allowances,
                deductions=deductions,
            )
        elif spec.contract == "voluntary":
            contracts.terminate(
                contract, on=ctx.days(-4), reason=TerminationReason.VOLUNTARY, by=None
            )
            worker.status = WorkerStatus.LEFT_JOB
            worker.save(update_fields=["status", "updated_at"])
        elif spec.contract == "company":
            contracts.terminate(
                contract,
                on=ctx.days(-9),
                reason=TerminationReason.COMPANY,
                note="事業縮小（ダミー）",
                by=None,
            )
            worker.status = WorkerStatus.LEFT_JOB
            worker.save(update_fields=["status", "updated_at"])


# 転籍の場面（9 番）: 食品の契約を D−60 で終え（転籍）、3 社目（飲食料品製造業分野）と
# 新しい契約を結ぶ
TRANSFER_NUMBER = 9


def seed_transfer(ctx: SeedContext) -> None:
    """3 社目（サンプル水産加工）と 9 番の転籍。

    3 社目は、意図した場面（新しい契約のキャリアアッププラン未交付）以外の警告が出ないように
    そろえる（全部委託・協議会の加入と証明書・協力確認書・事業所）。

    乱数は別の種を使い、既存のダミー（氏名・番号）の並びを変えない。
    """
    rng = random.Random(SEED + 1)
    food = SpecifiedSkillField.objects.get(code="food_manufacturing")
    host = HostOrganization.objects.create(
        name=f"{DUMMY_MARK}サンプル水産加工株式会社",
        name_kana="サンプルスイサンカコウ",
        corporate_number=corporate_number.generate_dummy(rng),
        representative_title="代表取締役",
        representative_name=f"代表 三郎{DUMMY_MARK}",
        postal_code="000-0004",
        address="静岡県ダミー浜市サンプル港1-1",
        phone_number="000-0000-0003",
    )
    ctx.keep(host)
    ctx.hosts.append(host)
    site = HostSite.objects.create(
        host_organization=host,
        name="水産加工工場",
        postal_code="000-0004",
        address="静岡県ダミー浜市サンプル港1-1",
        phone_number="000-0000-1099",
        industry_code="0929",  # その他の水産食料品製造業（中分類09 食料品製造業）
        field=food,
    )
    site.categories.set(_categories("food_manufacturing_general"))
    ctx.keep(site)
    ctx.extra_sites.append(site)
    membership = HostMembership.objects.create(
        host_organization=host,
        field=food,
        joined_on=ctx.days(-400),
        valid_until=None,
        member_number="DUMMY-0003",
    )
    ctx.keep(membership)
    ctx.keep(
        create_attachment(
            target=membership,
            kind=AttachmentKind.MEMBERSHIP_CERTIFICATE,
            file=ContentFile(
                dummy_pdf("Council membership certificate (host organization)"),
                name="dummy_membership.pdf",
            ),
            created_by=None,
        )
    )
    # 協力確認書: 事業所の所在地（ダミー浜市）と、9 番の住居地（サンプル市）。どちらも事業所の行
    for municipality in ("ダミー浜市", "サンプル市"):
        ctx.keep(
            HostCooperationConfirmation.objects.create(
                host_organization=host,
                site=site,
                municipality=municipality,
                submitted_on=ctx.days(-120),
            )
        )
    support = SupportContract.objects.create(
        host_organization=host,
        monthly_fee=30000,
        concluded_on=ctx.days(-380),
        starts_on=ctx.days(-365),
        ends_on=None,
    )
    ctx.keep(support)
    seed_fees(support)

    worker = ctx.worker(TRANSFER_NUMBER)
    spec = WORKER_SPECS[TRANSFER_NUMBER - 1]
    old = EmploymentContract.objects.get(worker=worker)
    contracts.terminate(old, on=ctx.days(-60), reason=TerminationReason.JOB_CHANGE, by=None)
    values = _version_values(ctx, site, spec, ctx.days(-59))
    new = _add_version(
        ctx,
        values,
        contract=None,
        worker=worker,
        allowances=[("住宅手当", 10000)],
        deductions=[("housing", 30000)],
    ).contract
    ctx.transferred_contract = new
    # 転籍の届出（終了・新たな契約）は提出済みにする（意図しない超過の警告を出さない）
    for filing in Filing.objects.filter(worker=worker, status=FilingStatus.NOT_STARTED):
        filings.transition(filing, FilingStatus.SUBMITTED, by=None, submitted_on=ctx.days(-50))


# 加入証明なしの場面（T39-D1）: 4 社目（サンプル部品工業。工業）に、加入はあるが構成員証明書が無い。
# 31 番（入国前）がその会社と雇用契約を結ぶ。第3-1号などが「証明書が添付されていません」で
# 生成不可になる。1〜30 番・3 社の場面は変えない（31 番は ctx.workers に入れず、支援計画のほかの
# 支援の記録は作らない）
UNCERTIFIED_NUMBER = 31


def seed_uncertified_host(ctx: SeedContext) -> None:
    """4 社目と 31 番。乱数は別の種を使い、既存のダミーの並びを変えない。"""
    rng = random.Random(SEED + UNCERTIFIED_NUMBER)
    industrial = SpecifiedSkillField.objects.get(code="industrial_products")
    host = HostOrganization.objects.create(
        name=f"{DUMMY_MARK}サンプル部品工業株式会社",
        name_kana="サンプルブヒンコウギョウ",
        corporate_number=corporate_number.generate_dummy(rng),
        representative_title="代表取締役",
        representative_name=f"代表 四郎{DUMMY_MARK}",
        postal_code="000-0005",
        address="岐阜県ダミー市サンプル町2-4-6",
        phone_number="000-0000-0004",
    )
    ctx.keep(host)
    ctx.uncertified_host = host
    site = HostSite.objects.create(
        host_organization=host,
        name="部品工場",
        postal_code="000-0005",
        address="岐阜県ダミー市サンプル町2-4-6",
        phone_number="000-0000-1098",
        industry_code="2699",  # その他の生産用機械・同部分品製造業（第3-1号の 76 の産業）
        field=industrial,
    )
    site.categories.set(_categories("machine_metal_processing"))
    ctx.keep(site)
    # 加入はあるが、構成員証明書を添付しない（この場面のため）
    ctx.keep(
        HostMembership.objects.create(
            host_organization=host,
            field=industrial,
            joined_on=ctx.days(-30),
            valid_until=None,
            member_number="DUMMY-0004",
        )
    )
    support = SupportContract.objects.create(
        host_organization=host,
        monthly_fee=30000,
        concluded_on=ctx.days(-14),
        starts_on=ctx.days(-7),
        ends_on=ctx.days(358),
    )
    ctx.keep(support)
    seed_fees(support)

    country = Country.objects.get(code="VN")
    worker = ForeignWorker.objects.create(
        name_romaji="NGUYEN VAN AN",
        name_kanji="",
        name_native="Nguyễn Văn An",
        nationality=country,
        birth_date=ctx.days(-365 * 25),
        sex=Sex.MALE,
        support_language=SupportLanguage.objects.get(code="ja"),
        assigned_staff=next((s for s in ctx.staff if not s.is_support_manager), ctx.staff[0]),
        status=WorkerStatus.PRE_ENTRY,
    )
    ctx.keep(worker)
    _staff_period(ctx, worker)
    ctx.uncertified_worker = worker
    _residence(
        ctx,
        worker,
        card_number="",
        status_of_residence=StatusOfResidence.SSW_1,
        application_type=ApplicationType.CERTIFICATE_OF_ELIGIBILITY,
        permitted_on=ctx.days(-10),
        expires_on=ctx.days(80),
    )
    ctx.keep(
        PassportRecord.objects.create(
            worker=worker,
            number=passport.generate_dummy(rng),
            issuing_country=country,
            issued_on=ctx.days(-400),
            expires_on=ctx.days(3250),
        )
    )
    ctx.keep(
        WorkerAddress.objects.create(
            worker=worker,
            postal_code="000-0005",
            address="岐阜県ダミー市サンプル町2-4-6 サンプル寮131号",
            effective_from=ctx.days(-10),
        )
    )
    shared, ctx.rng = ctx.rng, rng  # 基本給の乱数も別の種から取る
    try:
        values = _version_values(ctx, site, WorkerSpec("VN", None), ctx.days(30))
    finally:
        ctx.rng = shared
    contract = _add_version(
        ctx,
        values,
        contract=None,
        worker=worker,
        allowances=[("住宅手当", 10000)],
        deductions=[("housing", 30000)],
    ).contract
    # 支援計画（ほかの人と同じく標準の計画から。第1-17号も加入証明なしで生成不可になる）
    applies_from = values["starts_on"] - timedelta(days=30)
    content = support_plans.content_for_worker(worker)
    content.created_on = applies_from
    support_builders.fill_plan(content, applies_from)
    support_plans.create_plan(
        worker, contract.host_organization, applies_from=applies_from, by=None, content=content
    )


def seed_filings(ctx: SeedContext) -> None:
    """日付の到来で起票するもの（T11 の日次の起票を 1 回実行する）。"""
    filings.create_scheduled(ctx.base)


Step = Callable[[SeedContext], None]


def seed_career_plans(ctx: SeedContext) -> None:
    """飲食料品製造業分野の事業所を持つ受入れ機関（食品・水産加工）のキャリアアッププラン（T36）。"""
    food = SpecifiedSkillField.objects.get(code="food_manufacturing")
    levels = [
        career_plans.LevelInput(
            name="レベル1",
            years_from=1,
            years_to=2,
            duties=f"製造ラインの基本作業（指示を受けて行う）{DUMMY_MARK}",
            requirements="特定技能1号の評価試験の合格",
            position="一般",
            treatment="基本給の見直し（年1回）",
        ),
        career_plans.LevelInput(
            name="レベル2",
            years_from=3,
            years_to=4,
            duties=f"工程の段取り・後輩の指導{DUMMY_MARK}",
            requirements="衛生管理の社内研修の修了",
            position="ライン長補佐",
            treatment="役職手当",
        ),
        career_plans.LevelInput(
            name="レベル3",
            years_from=5,
            years_to=None,
            duties=f"工程の管理（特定技能2号の業務）{DUMMY_MARK}",
            requirements="特定技能2号の評価試験の合格・2年以上の工程管理の実務経験",
            position="ライン長",
            treatment="昇給・賞与の加算",
        ),
    ]
    for host in HostOrganization.objects.filter(pk__in=[h.pk for h in ctx.hosts]):
        if not host.sites.filter(field=food).exists():
            continue
        version = career_plans.create_plan(
            host,
            food,
            None,
            career_plans.VersionInput(
                applies_from=ctx.days(-900),
                career_route=f"作業員 → ライン長補佐 → ライン長（特定技能2号）{DUMMY_MARK}",
                levels=levels,
            ),
            by=None,
        )
        ctx.keep(version.plan)
        ctx.keep(version)
        for level in version.levels.all():
            ctx.keep(level)


def seed_condition_templates(ctx: SeedContext) -> None:
    """雇用条件のひな形（T41-D8）: 1 社目（サンプル精機工業。工業）の本社工場の 1 件。

    基本給・契約期間・入国予定日・雇入れ時の健康診断の日・業務の内容は外国人ごとに入れる印を付ける。
    外国人・契約・届出は作らない（ほかの場面は変わらない）。値は契約の版と同じ作り方で、乱数は
    別の文脈のものを使う（ctx.rng を進めると、後の場面の値が変わるため）。
    """
    host = ctx.hosts[0]
    site = host.sites.order_by("pk").first()
    assert site is not None
    values = _version_values(SeedContext(base=ctx.base), site, WorkerSpec("VN", 365), ctx.base)
    marked = ["base_wage", "starts_on", "ends_on", "expected_entry_on", "hiring_health_check_on"]
    marked.append("duties")
    for name in ("changed_on", "signed_on", "starts_on", "ends_on", "base_wage"):
        values.pop(name)
    values.pop("hourly_equivalent")  # 基本給から計算する（外国人ごと）
    payload = condition_templates.values_from(values, values)
    payload["categories"] = [category.pk for category in site.categories.order_by("pk")[:1]]
    allowance = {"name": "皆勤手当", "amount": 5000, "calculation_method": ""}
    template = EmploymentConditionTemplate(
        host_organization=host, name=f"製造ライン 正社員{DUMMY_MARK}"
    )
    condition_templates.save_template(
        template,
        values=payload,
        details={
            "shifts": [],
            "allowances": [allowance | {"fixed_overtime_hours": None}],
            "deductions": [{"kind": "housing", "name": "", "amount": 25000}],
        },
        marked=marked,
        allowed=[*payload, *marked],
        by=None,
    )
    ctx.keep(template)


STEPS: tuple[Step, ...] = (
    seed_organization,
    seed_staff,
    seed_hosts,
    seed_workers,
    seed_contracts,
    seed_transfer,
    seed_career_plans,
    seed_condition_templates,  # 雇用条件のひな形（T41）
    support_builders.seed_support,  # 段階2（支援計画・実績・相談・ガイダンス・交付・要配慮添付）
    seed_uncertified_host,  # 加入証明なしの場面（T39）
    bulk_builders.seed_ledger_items,  # 一括申請の台帳の項目と役員（段階4 T55）
    bulk_builders.seed_efile_filings,  # 一括届出の場面（段階4 T55）
    photo_builders.seed_photos,  # 申請用の写真（U4）
    seed_filings,
)


# --- 実行・リセット ------------------------------------------------------------------


def seeded_count() -> int:
    return SeedObject.objects.count()


@transaction.atomic
def run(base: date) -> SeedContext:
    ensure_allowed()
    ctx = SeedContext(base=base)
    for step in STEPS:
        step(ctx)
    return ctx


# 消す順番（依存の逆順）。台帳に無い行（届出タスク）は対象から辿って消す
DELETE_ORDER: tuple[type[models.Model], ...] = (
    Attachment,
    WorkerQualification,  # 段階4 T55
    HostOfficer,
    HostApplicantContact,
    HostDeclaration,
    HostFinancialYear,
    CareerPlanLevel,
    CareerPlanVersion,
    CareerPlan,
    EmploymentContractShift,
    EmploymentContractAllowance,
    EmploymentContractDeduction,
    EmploymentContractVersion,
    EmploymentContract,
    EmploymentConditionTemplate,
    ResidenceRecord,
    PassportRecord,
    WorkerAddress,
    WorkerContact,
    ForeignWorker,
    SupportContract,
    HostCooperationConfirmation,
    HostMembership,
    HostSite,
    HostOrganization,
    Staff,
    Office,
    Organization,
)


@transaction.atomic
def reset(
    entries: models.QuerySet[SeedObject] | None = None, *, scope: str = SeedScope.DUMMY
) -> int:
    """台帳に載っている行だけを消す。監査ログは追記専用なので残る（削除も記録される）。

    entries を省くとダミーの行（seed_dummy）。テストシナリオの行（seed_scenario）はダミーの担当者・
    自機関を参照するので、ダミーを消すときは先に消す。

    雇用契約の版・明細はモデルの delete() では消せない（T09）。開発用のこの処理に限り、
    QuerySet の一括削除で消す。
    """
    ensure_allowed()
    deleted_scenario = 0
    if entries is None:
        if scope == SeedScope.DUMMY and SeedObject.scenario_objects.exists():
            deleted_scenario = reset(SeedObject.scenario_objects.all(), scope=SeedScope.SCENARIO)
        entries = SeedObject.objects.all()
    by_model: dict[type[models.Model], list[int]] = {}
    for entry in entries.select_related("content_type"):
        model = entry.content_type.model_class()
        if model is not None:
            by_model.setdefault(model, []).append(entry.object_id)
    deleted = delete_rows(by_model)
    SeedObject.all_objects.filter(scope=scope).delete()
    return deleted + deleted_scenario


def delete_rows(by_model: dict[type[models.Model], list[int]]) -> int:
    """種類ごとの ID の行を、紐づく支援のデータ・届出タスク・添付のファイルとあわせて消す。

    開発用。

    台帳の行（reset）と、seed_scenario の --replace（画面で作った本線と同じ記録）から使う。
    台帳の外の行でも、台帳の外国人・受入れ機関・事業所に紐づくもの（支援のデータ・生成物・
    統合 PDF の生成履歴と下書き・受入れ機関の書類の保管庫・雇用条件のひな形）は消す。
    監査ログは消さない。
    """
    # 支援のデータは外国人に紐づくものをまとめて消す（届出タスクより先。定期面談の組が
    # 届出タスクを参照しているため）。手で足した記録も、その外国人のものなら消える
    deleted_support = support_builders.delete_support_data(by_model.get(ForeignWorker, []))
    deleted_support += support_builders.delete_host_documents(by_model.get(HostOrganization, []))
    # 受入れ機関・事業所の書類の保管庫（画面で足したものも、その受入れ機関・事業所のものなら消す）
    deleted_support += support_builders.delete_host_store(
        by_model.get(HostOrganization, []), by_model.get(HostSite, [])
    )
    # 画面で足した雇用条件のひな形も、その受入れ機関のものなら消す（T41）
    deleted_support += EmploymentConditionTemplate.objects.filter(
        host_organization_id__in=by_model.get(HostOrganization, [])
    ).delete()[0]
    targets = [(ContentType.objects.get_for_model(model), ids) for model, ids in by_model.items()]
    filings_query = models.Q(worker_id__in=by_model.get(ForeignWorker, []))
    for content_type, ids in targets:
        filings_query |= models.Q(content_type=content_type, object_id__in=ids)
    # 一括申請・一括届出の行（外国人・届出タスクを参照する。段階4）
    worker_ids = by_model.get(ForeignWorker, [])
    deleted_support += BulkFilingRow.objects.filter(
        models.Q(worker_id__in=worker_ids)
        | models.Q(filing__in=Filing.objects.filter(filings_query))
    ).delete()[0]
    deleted_support += BulkApplicationEntry.objects.filter(worker_id__in=worker_ids).delete()[0]
    deleted = Filing.objects.filter(filings_query).delete()[0]
    for attachment in Attachment.objects.filter(pk__in=by_model.get(Attachment, [])):
        attachment.file.delete(save=False)
    # 報酬の説明は版ごとに 1 つ（画面で入れ直したものも、その版のものなら消す）
    deleted += ContractWageExplanation.objects.filter(
        version_id__in=by_model.get(EmploymentContractVersion, [])
    ).delete()[0]
    # 担当者・支援責任者の履歴は外国人・事務所と一緒に消えるが、担当者を先に消すので先に消す（T45）
    staff_ids = by_model.get(Staff, [])
    deleted += StaffAssignment.objects.filter(
        models.Q(worker_id__in=by_model.get(ForeignWorker, [])) | models.Q(staff_id__in=staff_ids)
    ).delete()[0]
    deleted += OfficeManagerPeriod.objects.filter(
        models.Q(office_id__in=by_model.get(Office, [])) | models.Q(staff_id__in=staff_ids)
    ).delete()[0]
    # 申請用の写真（U4）: ダミーの外国人の写真は、画面で足したものも消す（ファイルも消す）
    for photo in WorkerPhoto.objects.filter(worker_id__in=by_model.get(ForeignWorker, [])):
        photo.original.delete(save=False)
        photo.cropped.delete(save=False)
        deleted += photo.delete()[0]
    for model in DELETE_ORDER:
        ids = by_model.get(model, [])
        if ids:
            deleted += model._base_manager.filter(pk__in=ids).delete()[0]
    return deleted + deleted_support
