"""外国人一覧の検索・絞り込み・並べ替え（第5章 F01、T13b）。

- 氏名は 3 表記（ローマ字・漢字・母国語）の部分一致。全角・半角、大小文字、空白の有無を
  揃えて比べる。
- 在留カード番号・旅券番号は完全一致だけ（ブラインドインデックス。部分一致はできない）。
- 受入れ機関は、今日有効な雇用契約の機関。無ければ開始前（予定）の契約の機関。
  分野はその契約の最新の版の就業場所から取る。
- 警告レベルは最新の在留情報の在留期限から T10 の residence_expiry / assess で求める。
- クエリの数は人数によらず一定（外国人 1 回・分野 1 回・データ範囲の判定）。
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from django.db.models import Case, F, IntegerField, OuterRef, Q, QuerySet, Subquery, Value, When
from django.db.models.functions import Replace, Upper

from apps.contracts.models import EmploymentContract, EmploymentContractVersion
from apps.core.models import User
from apps.hosts.models import SpecifiedSkillField
from apps.workers.models import ApplicationType, ForeignWorker, WorkerStatus
from services import authorization, deadlines, workers

# 既定で一覧に出す状態（離職・帰国は状態で絞り込んだときだけ出す）
DEFAULT_STATUSES: tuple[str, ...] = (WorkerStatus.PRE_ENTRY, WorkerStatus.RESIDING)

# 母国語の氏名の言語（ブラウザに字形を選ばせるため。lang 属性に使う）
NATIVE_NAME_LANGUAGES: dict[str, str] = {"CN": "zh-Hans"}


class Order:
    EXPIRY = "expiry"  # 在留期限の近い順（期限の無い入国前は最後）
    NAME = "name"  # 氏名（ローマ字）順


@dataclass(frozen=True)
class WorkerListQuery:
    text: str = ""
    host_organization_id: int | None = None
    assigned_staff_id: int | None = None
    unassigned_only: bool = False
    statuses: tuple[str, ...] = DEFAULT_STATUSES  # 空なら状態で絞らない
    field_id: int | None = None
    level: deadlines.Level | None = None
    order: str = Order.EXPIRY
    include_deactivated: bool = False  # 無効化済みも出す（管理者だけが選べる）


@dataclass(frozen=True)
class WorkerRow:
    worker: ForeignWorker
    display_name: str  # 漢字、無ければ母国語、どちらも無ければローマ字
    display_name_lang: str  # display_name が母国語のときの lang（それ以外は空）
    romaji: str  # display_name がローマ字のときは空（同じ表記を 2 回出さない）
    expires_on: date | None
    deadline: deadlines.Deadline | None
    level: deadlines.Level | None
    host_organization_id: int | None
    host_organization_name: str
    contract_scheduled: bool  # 受入れ機関が開始前の契約のもの
    skill_field: SpecifiedSkillField | None


@dataclass(frozen=True)
class WorkerList:
    rows: list[WorkerRow]
    level_counts: Counter[deadlines.Level] = field(default_factory=Counter)
    total: int = 0  # 期限のレベルで絞る前の人数（一覧のタブ「すべて」の件数）


def normalize_name(value: str) -> str:
    """氏名の比較用（全角→半角、大文字、空白を除く。ひらがなはカタカナにする。修正 1 回目 A4）。"""
    text = "".join(unicodedata.normalize("NFKC", value).upper().split())
    return "".join(chr(ord(c) + 0x60) if "\u3041" <= c <= "\u3096" else c for c in text)


def _compact(field_name: str) -> Replace:
    """DB 側で空白（半角・全角）を除き大文字にした氏名。"""
    without_spaces = Replace(
        Replace(Upper(field_name), Value(" "), Value("")), Value("　"), Value("")
    )
    return without_spaces


def _matches_text(queryset: QuerySet[ForeignWorker], text: str) -> QuerySet[ForeignWorker]:
    needle = normalize_name(text)
    if not needle:
        return queryset
    by_name = Q(romaji_compact__contains=needle) | Q(kanji_compact__contains=needle)
    by_name |= Q(native_compact__contains=needle) | Q(kana_compact__contains=needle)
    by_number = Q(pk__in=workers.worker_ids_by_document_number(text))
    return queryset.annotate(
        romaji_compact=_compact("name_romaji"),
        kanji_compact=_compact("name_kanji"),
        native_compact=_compact("name_native"),
        kana_compact=_compact("name_kana"),
    ).filter(by_name | by_number)


def _current_contract(today: date) -> QuerySet[EmploymentContract]:
    """現在の雇用契約（有効 → 予定の順。終了の登録済みは除く）。外国人ごとのサブクエリで使う。"""
    return (
        EmploymentContract.objects.filter(worker=OuterRef("pk"), terminated_on__isnull=True)
        .annotate(
            is_scheduled=Case(
                When(current_starts_on__gt=today, then=Value(1)),
                default=Value(0),
                output_field=IntegerField(),
            )
        )
        .order_by("is_scheduled", "-current_starts_on", "-pk")
    )


def _latest_site_field() -> Subquery:
    """現在の雇用契約の最新の版の就業場所の分野。"""
    return Subquery(
        EmploymentContractVersion.objects.filter(contract=OuterRef("current_contract_id"))
        .order_by("-version")
        .values("site__field_id")[:1]
    )


def display_name(worker: ForeignWorker) -> tuple[str, str, str]:
    """一覧・詳細の氏名の表示（表示名、母国語のときの lang、併記するローマ字）。"""
    if worker.name_kanji:
        return worker.name_kanji, "", worker.name_romaji
    if worker.name_native:
        language = NATIVE_NAME_LANGUAGES.get(worker.nationality.code, "")
        return worker.name_native, language, worker.name_romaji
    return worker.name_romaji, "", ""


def list_workers(user: User, query: WorkerListQuery, today: date) -> WorkerList:
    base = ForeignWorker.objects.all() if query.include_deactivated else workers.active_workers()
    queryset = authorization.scope(user, base).select_related("nationality", "assigned_staff")
    if query.statuses:
        queryset = queryset.filter(status__in=query.statuses)
    if query.unassigned_only:
        queryset = queryset.filter(assigned_staff__isnull=True)
    elif query.assigned_staff_id is not None:
        queryset = queryset.filter(assigned_staff_id=query.assigned_staff_id)
    queryset = _matches_text(queryset, query.text)
    contracts = _current_contract(today)
    annotated = queryset.annotate(
        expires_on=F("latest_residence__record__expires_on"),
        application_type=F("latest_residence__record__application_type"),
        card_number_index=F("latest_residence__record__card_number_index"),
        current_contract_id=Subquery(contracts.values("pk")[:1]),
        host_id=Subquery(contracts.values("host_organization_id")[:1]),
        host_name=Subquery(contracts.values("host_organization__name")[:1]),
        contract_starts_on=Subquery(contracts.values("current_starts_on")[:1]),
    ).annotate(skill_field_id=_latest_site_field())
    if query.host_organization_id is not None:
        annotated = annotated.filter(host_id=query.host_organization_id)
    if query.field_id is not None:
        annotated = annotated.filter(skill_field_id=query.field_id)
    if query.order == Order.NAME:
        annotated = annotated.order_by("name_romaji", "pk")
    else:
        annotated = annotated.order_by(F("expires_on").asc(nulls_last=True), "name_romaji", "pk")

    skill_fields = {
        skill_field.pk: skill_field for skill_field in SpecifiedSkillField.objects.all()
    }
    rows: list[WorkerRow] = []
    counts: Counter[deadlines.Level] = Counter()
    total = 0
    for worker in annotated:
        total += 1
        expires_on: date | None = worker.expires_on
        # 在留資格認定証明書の行（在留カード番号なし）の期限は、認定証明書の有効期限で在留期限では
        # ない（修正 1 回目 B5）。認定証明書で入国して在留カードの番号がある行は在留期限
        before_entry = (
            worker.application_type == ApplicationType.CERTIFICATE_OF_ELIGIBILITY
            and not worker.card_number_index
        )
        if before_entry:
            expires_on = None
        deadline = deadlines.residence_expiry(expires_on) if expires_on else None
        level = deadlines.assess(deadline, today).level if deadline else None
        if level is not None:
            counts[level] += 1
        if query.level is not None and level is not query.level:
            continue
        name, name_lang, romaji = display_name(worker)
        rows.append(
            WorkerRow(
                worker=worker,
                display_name=name,
                display_name_lang=name_lang,
                romaji=romaji,
                expires_on=expires_on,
                deadline=deadline,
                level=level,
                host_organization_id=worker.host_id,
                host_organization_name=worker.host_name or "",
                contract_scheduled=(
                    worker.contract_starts_on is not None and worker.contract_starts_on > today
                ),
                skill_field=skill_fields.get(worker.skill_field_id),
            )
        )
    return WorkerList(rows=rows, level_counts=counts, total=total)
