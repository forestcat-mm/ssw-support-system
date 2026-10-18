"""受入れ機関の業務ロジック（業務区分の検査・事業所の編集の制限・加入状況・協力確認書の提出状況・
支援委託契約の数）。保存と期限のタスクの処理は services.host_records。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils.translation import gettext as _

from apps.contracts.models import EmploymentContract, EmploymentContractVersion
from apps.hosts.models import (
    SUPPORT_TERMINATION_REASONS,
    EntityType,
    HostCooperationConfirmation,
    HostMembership,
    HostOrganization,
    HostSite,
    OperationCategory,
    SpecifiedSkillField,
    SupportContract,
    SupportTerminationReason,
)
from apps.workers.models import ForeignWorker, ResidenceRecord, StatusOfResidence


def validate_site_categories(
    field: SpecifiedSkillField | None, categories: Iterable[OperationCategory]
) -> None:
    """事業所の業務区分は、事業所の分野に属するものだけ。"""
    others = [c for c in categories if field is None or c.field_id != field.pk]
    if others:
        raise ValidationError(
            _("These operation categories do not belong to the site's field: %(names)s")
            % {"names": ", ".join(str(c) for c in others)},
            code="category_field",
        )


def validate_corporate_number_for(entity_type: str, number: str) -> None:
    """法人は法人番号が必須。個人事業主は法人番号を持たないので入力させない。

    DB の検査制約（hosts_hostorganization_*_number）と同じ規則。
    """
    if entity_type == EntityType.SOLE_PROPRIETOR and number:
        raise ValidationError(
            {"corporate_number": _("A sole proprietor has no corporate number. Leave it blank.")},
            code="sole_proprietor_number",
        )
    if entity_type == EntityType.CORPORATION and not number:
        raise ValidationError(
            {"corporate_number": _("Enter the corporate number of a corporation.")},
            code="corporation_number",
        )


def versions_in_use_at(site: HostSite, today: date) -> list[EmploymentContractVersion]:
    """その事業所を就業の場所にしている、今日有効な版と今後適用される版（T28a の D5）。

    対象の契約は、終了が未登録か、終了日が今日以後のもの（期間が過ぎても終了を登録していない契約と、
    予定の契約を含む。安全側）。契約ごとに、今日有効な版（変更日が今日以前の最も新しい版。無ければ
    第1版）と、変更日が今日より後の版をすべて見て、この事業所を参照している版を返す。
    """
    if site.pk is None:
        return []
    contract_ids = set(
        EmploymentContractVersion.objects.filter(site_id=site.pk)
        .filter(Q(contract__terminated_on__isnull=True) | Q(contract__terminated_on__gte=today))
        .values_list("contract_id", flat=True)
    )
    result: list[EmploymentContractVersion] = []
    for contract_id in sorted(contract_ids):
        versions = list(
            EmploymentContractVersion.objects.filter(contract_id=contract_id)
            .prefetch_related("categories")
            .order_by("version")
        )
        applied = [version for version in versions if version.changed_on <= today]
        in_effect = applied[-1] if applied else versions[0]
        relevant = [in_effect] + [
            version
            for version in versions
            if version.changed_on > today and version.pk != in_effect.pk
        ]
        result.extend(version for version in relevant if version.site_id == site.pk)
    return result


def validate_site_change(
    site: HostSite,
    *,
    field: SpecifiedSkillField | None,
    categories: Iterable[OperationCategory],
    is_active: bool,
    today: date,
) -> None:
    """雇用契約が参照している事業所の編集の制限（T28a の D5。ADR-0015）。

    - いずれかの版が参照している事業所は、分野を変えられない（版の業務区分・キャリアアッププランの
      判定が事業所の分野に依存するため）
    - 今日有効な版・今後適用される版（versions_in_use_at）が使っている業務区分は外せない
      （次の版の検査 validate_categories_of_site が通らなくなる）
    - それらの版が参照している事業所は無効にできない
    """
    if site.pk is None:
        return
    errors: dict[str, list[ValidationError]] = {}
    saved = HostSite.objects.get(pk=site.pk)
    if (
        field is not None
        and field.pk != saved.field_id
        and EmploymentContractVersion.objects.filter(site_id=site.pk).exists()
    ):
        errors.setdefault("field", []).append(
            ValidationError(
                _(
                    "The field cannot be changed because employment contracts use this site "
                    "as the workplace."
                ),
                code="site_in_use_field",
            )
        )
    current = versions_in_use_at(site, today)
    selected = {category.pk for category in categories}
    used = {
        category
        for version in current
        for category in version.categories.all()
        if category.pk not in selected
    }
    if used:
        errors.setdefault("categories", []).append(
            ValidationError(
                _(
                    "These operation categories are used by the version of an employment contract "
                    "in effect today or to be applied later: %(names)s"
                )
                % {"names": ", ".join(sorted(str(category) for category in used))},
                code="site_in_use_category",
            )
        )
    if not is_active and current:
        errors.setdefault("is_active", []).append(
            ValidationError(
                _(
                    "This site cannot be deactivated because the version of an employment "
                    "contract in effect today or to be applied later uses it as the workplace."
                ),
                code="site_in_use_active",
            )
        )
    if errors:
        raise ValidationError(errors)


def validate_cooperation_site(host_id: int | None, site: HostSite | None) -> None:
    """協力確認書の事業所は、その受入れ機関の事業所であること。"""
    if host_id is not None and site is not None and site.host_organization_id != host_id:
        raise ValidationError(
            {"site": _("Select a site of this host organization.")}, code="site_host"
        )


@dataclass(frozen=True)
class CooperationStatus:
    """事業所の協力確認書の提出状況（提出先ごとの最新の提出）。空なら未提出。"""

    site: HostSite
    latest: list[HostCooperationConfirmation]


def cooperation_status(sites: Iterable[HostSite]) -> list[CooperationStatus]:
    """事業所ごとに、提出先の市区町村ごとの最新の提出（提出日の新しい順で 1 件）を返す。

    提出先と事業所の所在地の一致は見ない（住居地の市区町村への提出も同じ事業所の行で持つ。
    生成不可・警告の判定は T30）。
    """
    site_list = list(sites)
    rows: dict[int, dict[str, HostCooperationConfirmation]] = {site.pk: {} for site in site_list}
    for confirmation in HostCooperationConfirmation.objects.filter(site__in=site_list).order_by(
        "-submitted_on", "-pk"
    ):
        rows[confirmation.site_id].setdefault(confirmation.municipality, confirmation)
    return [
        CooperationStatus(
            site=site,
            latest=sorted(rows[site.pk].values(), key=lambda c: (c.municipality, c.pk)),
        )
        for site in site_list
    ]


def membership_on(
    host: HostOrganization, field: SpecifiedSkillField, on: date
) -> HostMembership | None:
    """その日に有効な加入状況（加入日が on 以前で、有効期限が空または on 以後）。"""
    return (
        host.memberships.filter(field=field, joined_on__lte=on)
        .filter(Q(valid_until__isnull=True) | Q(valid_until__gte=on))
        .first()
    )


def is_member(host: HostOrganization, field: SpecifiedSkillField, on: date) -> bool:
    return membership_on(host, field, on) is not None


def validate_contract_period(
    host_id: int | None,
    starts_on: date | None,
    ends_on: date | None,
    *,
    exclude_pk: int | None = None,
    terminated_on: date | None = None,
) -> None:
    """同じ受入れ機関の契約と期間が重ならないこと（両端を含む。終了日が空なら期限なし）。

    期間の末日は、実際の終了日（終了の登録）があればそれ、無ければ予定の終了日（ADR-0017）。
    DB の排他制約と同じ判定を保存前に行う（admin のインラインでは制約の検査が飛ばされるため）。
    """
    if host_id is None or starts_on is None:
        return
    if ends_on is not None and ends_on < starts_on:
        raise ValidationError(_("The end date must be on or after the start date."), code="period")
    end = terminated_on or ends_on
    overlapping = SupportContract.objects.filter(host_organization_id=host_id).filter(
        _ends_on_or_after(starts_on)
    )
    if end is not None:
        overlapping = overlapping.filter(starts_on__lte=end)
    if exclude_pk is not None:
        overlapping = overlapping.exclude(pk=exclude_pk)
    if overlapping.exists():
        raise ValidationError(
            _("This period overlaps another support contract of the same host organization."),
            code="overlap",
        )


def periods_overlap(a: tuple[date, date | None], b: tuple[date, date | None]) -> bool:
    """両端を含む期間の重なり（終了日が None なら期限なし）。"""
    (a_start, a_end), (b_start, b_end) = a, b
    return (a_end is None or a_end >= b_start) and (b_end is None or b_end >= a_start)


def _ends_on_or_after(on: date) -> Q:
    """期間の末日（実際の終了日、無ければ予定の終了日。空は期限なし）が on 以後。"""
    return (Q(terminated_on__isnull=True) | Q(terminated_on__gte=on)) & (
        Q(ends_on__isnull=True) | Q(ends_on__gte=on)
    )


def _active_on_q(on: date) -> Q:
    return Q(starts_on__lte=on) & _ends_on_or_after(on)


def previous_row(contract: SupportContract) -> SupportContract | None:
    """続く行の前の行（ADR-0017）: 同じ受入れ機関で、終了の登録が無く、予定の終了日の翌日に
    この行が始まる行。無ければ None（この行は新しい締結）。"""
    if contract.starts_on is None:
        return None
    return (
        SupportContract.objects.filter(
            host_organization_id=contract.host_organization_id,
            terminated_on__isnull=True,
            ends_on=contract.starts_on - timedelta(days=1),
        )
        .exclude(pk=contract.pk)
        .first()
    )


def validate_termination(
    contract: SupportContract,
    *,
    terminated_on: date | None,
    category: str,
    reason: str,
    note: str,
) -> None:
    """支援委託契約の終了の登録（第3-3-2号 A の終了年月日・事由）。"""
    errors: dict[str, list[str]] = {}

    def add(field: str, message: str) -> None:
        errors.setdefault(field, []).append(message)

    if contract.terminated_on is not None:
        add("terminated_on", _("The termination is already registered."))
    if terminated_on is None:
        add("terminated_on", _("Enter the termination date."))
    elif terminated_on < contract.starts_on or (
        contract.ends_on is not None and terminated_on > contract.ends_on
    ):
        add("terminated_on", _("The termination date must be within the contract period."))
    if category not in SUPPORT_TERMINATION_REASONS:
        add("termination_category", _("Select the termination category."))
    elif reason not in SUPPORT_TERMINATION_REASONS[category]:
        add("termination_reason", _("Select a reason that belongs to the category."))
    if reason == SupportTerminationReason.OTHER and not note.strip():
        add("termination_note", _("Enter the reason (up to 20 characters)."))
    if reason != SupportTerminationReason.OTHER and note.strip():
        add("termination_note", _("Only for the reason “other”."))
    if errors:
        raise ValidationError(errors)


def support_targets(host: HostOrganization, on: date) -> list[ForeignWorker]:
    """支援委託契約の届出の対象の外国人（ADR-0017）。

    前提: 台帳に載っていて、その受入れ機関と雇用契約がある 1号の人は、全員が自機関の支援の対象
    （外国人ごとの委託〔第5-10号の丙〕は T37 で見直す）。対象は、on の日に
    - その受入れ機関との雇用契約が有効（開始日が on 以前、期間の末日〔版の終了日と終了の登録日の
      早いほう〕が空か on 以後）で、
    - on の日に適用されている在留情報（取り消していない行のうち、許可日が on 以前の最も新しい行）の
      在留資格が特定技能1号の人。無効化した外国人は除く。
    申請の前の事由（在留資格がまだ特定技能でない）は届出が要らない（運用要領 本体 印刷 100頁）。
    """
    workers: dict[int, ForeignWorker] = {}
    contracts = EmploymentContract.objects.filter(
        host_organization=host, current_starts_on__lte=on, worker__deactivated_at__isnull=True
    ).select_related("worker")
    for contract in contracts:
        ends = [d for d in (contract.current_ends_on, contract.terminated_on) if d is not None]
        if ends and min(ends) < on:
            continue
        record = (
            ResidenceRecord.objects.filter(
                worker_id=contract.worker_id, voided_at__isnull=True, permitted_on__lte=on
            )
            .order_by("-permitted_on", "-pk")
            .first()
        )
        if record is not None and record.status_of_residence == StatusOfResidence.SSW_1:
            workers[contract.worker_id] = contract.worker
    return sorted(workers.values(), key=lambda worker: worker.pk)


def supported_worker_ids(on: date) -> set[int]:
    """その日に支援委託契約が有効な受入れ機関の、支援の対象の外国人（第1-17号Ⅲ欄７の人数の元）。"""
    ids: set[int] = set()
    contracts = SupportContract.objects.filter(_active_on_q(on)).select_related("host_organization")
    for contract in contracts:
        ids.update(worker.pk for worker in support_targets(contract.host_organization, on))
    return ids


def active_contract(host: HostOrganization, on: date) -> SupportContract | None:
    return host.support_contracts.filter(_active_on_q(on)).first()


def active_contract_count(on: date) -> int:
    """その日に支援委託契約が有効な受入れ機関の数（2027 年要件の「担当機関数」の元）。"""
    return int(
        SupportContract.objects.filter(_active_on_q(on))
        .values("host_organization")
        .distinct()
        .count()
    )
