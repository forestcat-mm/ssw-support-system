"""定期届出の第3-6号・別紙1・別紙2 の値（T51。ADR-0041）。

元は ADR-0012 の定期届出の元データ（services.periodic_report.support_rows）と、T45 の担当者の履歴。
受入れ機関×年度で作る。別紙1 は事業所ごと・5 人ごと（様式の行数）。

- 第3-6号: 届出対象期間・特定技能所属機関（法人番号の 13 マス・最も多い分野と分野の数・ふりがな・
  名称・住所・電話）・報告対象の人数（台帳の 1号の外国人。2号は台帳に持たないので 0）・
  支援の実施状況。
  ４ 労働条件等・６ 備考・担当者・署名は受入れ機関が記入する（給与の記録を持たない。T51-D3）。
- 別紙1: 機関名・事業所名と、1 人ごとに氏名・国籍・生年月日・性別・在留カード番号・受入れ分野・
  支援の形態（3月31日時点）・支援担当者と電話・備考（期間中の受入れ開始日・終了日）。活動日数・報酬・支払方法・
  随時届出の提出状況は空（受入れ機関と人が記入する。T51-D5）。
- 別紙2: 対象期間・受入れ機関の名称と法人番号・登録支援機関名と登録番号（自機関）。署名と日付は空。

在留カード番号は生成のときに復号する（生成履歴のスナップショットは暗号化。監査ログに生成を残す）。
"""

from __future__ import annotations

import datetime
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Q

from apps.core.models import Organization, User
from apps.forms_engine.models import GeneratedDocument
from apps.hosts.models import HostOrganization, HostSite
from apps.workers.models import ResidenceRecord, StatusOfResidence
from services import form_generation, periodic_report

ZEN = str.maketrans("0123456789", "０１２３４５６７８９")
REPORT_FORM_ID = "3-6"
SHEET_FORM_ID = "3-6-1"
SIGNATURE_FORM_ID = "3-6-2"
FORM_IDS = (REPORT_FORM_ID, SHEET_FORM_ID, SIGNATURE_FORM_ID)
PEOPLE_PER_SHEET = 5  # 別紙1 の様式の行数
CORPORATE_BOXES = 13


@dataclass(frozen=True)
class Line:
    """別紙1 の 1 人（support_rows の行と、期間中の受入れ開始日の推定の印）。"""

    row: periodic_report.SupportRow
    accepted_from: datetime.date | None  # 期間中に受入れを開始したときの開始日（備考）
    accepted_from_estimated: bool  # 許可日が台帳に無く、雇用契約の開始日で代えた
    accepted_until: datetime.date | None  # 期間中に受入れを終了した日（備考）


def _zen(value: int | str) -> str:
    return str(value).translate(ZEN)


def _japanese_date(day: datetime.date) -> str:
    return f"{day.year}年{day.month}月{day.day}日"


def acceptance_start(
    row: periodic_report.SupportRow, period: periodic_report.Period
) -> tuple[datetime.date | None, bool]:
    """期間中に受入れを開始した日（T51-D4）と、推定かどうか。

    作成要領（001454514.pdf PDF 3 頁 ※）: 受入れ開始日は「特定技能」の上陸許可・
    在留資格変更許可の日。
    期間中の特定技能1号の許可日が台帳にあればその日、無ければ雇用契約の開始日（推定）。
    期間より前から受け入れていれば None。
    """
    permitted = (
        ResidenceRecord.objects.filter(
            worker=row.worker,
            status_of_residence=StatusOfResidence.SSW_1,
            permitted_on__gte=period.start,
            permitted_on__lte=period.end,
        )
        .order_by("permitted_on", "pk")
        .values_list("permitted_on", flat=True)
        .first()
    )
    earlier = ResidenceRecord.objects.filter(
        worker=row.worker,
        status_of_residence=StatusOfResidence.SSW_1,
        permitted_on__lt=period.start,
    ).exists()
    if earlier:
        return None, False
    if permitted is not None:
        return permitted, False
    if row.accepted_from >= period.start:
        return row.accepted_from, True
    return None, False


def lines_of(host: HostOrganization, fiscal_year: int) -> list[Line]:
    period = periodic_report.report_period(fiscal_year)
    lines = []
    for row in periodic_report.support_rows(fiscal_year):
        if row.host.pk != host.pk:
            continue
        start, estimated = acceptance_start(row, period)
        lines.append(Line(row, start, estimated, row.accepted_until))
    return lines


def sheets_of(lines: Sequence[Line]) -> list[tuple[HostSite, list[Line]]]:
    """事業所ごと・5 人ごとの別紙1。"""
    by_site: dict[int, list[Line]] = defaultdict(list)
    sites: dict[int, HostSite] = {}
    for line in lines:
        by_site[line.row.site.pk].append(line)
        sites[line.row.site.pk] = line.row.site
    result = []
    for pk in sorted(by_site):
        site_lines = by_site[pk]
        for start in range(0, len(site_lines), PEOPLE_PER_SHEET):
            result.append((sites[pk], site_lines[start : start + PEOPLE_PER_SHEET]))
    return result


def _period_text(fiscal_year: int) -> str:
    return f"{_zen(fiscal_year)}年４月１日　～　{_zen(fiscal_year + 1)}年３月３１日"


def _postal(code: str) -> str:
    digits = "".join(char for char in code if char.isdigit())
    return f"〒{digits[:3]}－{digits[3:]}" if len(digits) == 7 else "〒"


def host_values(host: HostOrganization) -> dict[str, Any]:
    values: dict[str, Any] = {
        "h.name": host.name,
        "h.name_kana": host.name_kana,
        "h.postal": _postal(host.postal_code),
        "h.address": host.address,
        "h.phone": f"（電話　{host.phone_number}　）" if host.phone_number else "",
    }
    number = host.corporate_number
    if len(number) == CORPORATE_BOXES:
        values |= {f"h.corporate_number_{i}": char for i, char in enumerate(number, 1)}
    return values


def build_report(host: HostOrganization, fiscal_year: int, lines: Sequence[Line]) -> dict[str, Any]:
    """第3-6号の値。"""
    workers = {line.row.worker.pk for line in lines}
    fields = Counter(line.row.site.field.name for line in lines)
    values = host_values(host) | {
        "r.fiscal_year": fiscal_year,
        "r.period": _period_text(fiscal_year),
        "r.count_total": f"{len(workers)}人",
        "r.count_1": f"{len(workers)}人",
        "r.count_2": "0人",
        "r.own_support_only": False,
        # 全部委託の行（support_rows）が 1 人でもあれば「支援の全部委託あり」（作成要領 PDF 12 頁）
        "r.full_entrustment": bool(lines),
    }
    if fields:
        values["h.field"] = fields.most_common(1)[0][0]
        values["h.field_count"] = str(len(fields))
    return values


def _remarks(line: Line) -> str:
    parts = []
    if line.accepted_from is not None:
        parts.append(f"受入れ開始日\n{_japanese_date(line.accepted_from)}")
    if line.accepted_until is not None:
        parts.append(f"受入れ終了日\n{_japanese_date(line.accepted_until)}")
    return "\n".join(parts)


def build_sheet(
    host: HostOrganization, site: HostSite, fiscal_year: int, lines: Sequence[Line], part: int
) -> dict[str, Any]:
    """別紙1（1 枚・5 人まで）の値。"""
    values: dict[str, Any] = {
        "r.fiscal_year": fiscal_year,
        "r.part": part,
        "h.name": host.name,
        "s.name": site.name,
    }
    for number, line in enumerate(lines, 1):
        row = line.row
        worker = row.worker
        prefix = f"w{number}."
        card = ""
        if row.residence_record_id is not None:
            record = ResidenceRecord.objects.filter(pk=row.residence_record_id).first()
            card = record.card_number if record else ""
        values |= {
            prefix + "worker_id": worker.pk,
            prefix + "name": worker.name_romaji.upper(),
            prefix + "nationality": worker.nationality.name,
            prefix + "birth_date": worker.birth_date,
            prefix + "male": worker.sex == "male",
            prefix + "female": worker.sex == "female",
            prefix + "card": card,
            prefix + "field": site.field.name,
            prefix + "staff": row.staff.name if row.staff else "",
            prefix + "staff_phone": row.staff_phone,
            prefix + "remarks": _remarks(line),
        }
        if row.fully_outsourced_on_reference:
            values |= {
                prefix + "own_support": False,
                prefix + "full_entrustment": True,
                prefix + "organization": row.organization_name,
            }
    return values


def build_signature(host: HostOrganization, fiscal_year: int) -> dict[str, Any]:
    """別紙2（署名欄）の値。"""
    organization = Organization.objects.first()
    fill = "　"
    heading = (
        "　出入国管理及び難民認定法第19条の18第2項第1号、第2号及び第3号の規定又は同法第19条の30"
        "第1項の規定により、\n\n"
        f"{fill}{_zen(fiscal_year)}年度（{_zen(fiscal_year)}年４月１日　～　"
        f"{_zen(fiscal_year + 1)}年３月３１日）を対象期間とする\n\n"
        f"　特定技能所属機関：{host.name}に係る届出書について、その内容を確認した上で提出します。"
    )
    return {
        "r.fiscal_year": fiscal_year,
        "r.signature_heading": heading,
        "h.name_bracket": _bracket("特定技能所属機関の氏名又は名称", host.name, 32),
        "h.corporate_number_bracket": _bracket("法人番号（法人の場合）", host.corporate_number, 18),
        "o.name_bracket": _bracket("登録支援機関名", organization.name if organization else "", 29),
        "o.registration_bracket": _bracket(
            "登録番号", organization.registration_number if organization else "", 23
        ),
    }


def _bracket(label: str, value: str, width: int) -> str:
    """【見出し：値　…】。閉じ括弧の位置を様式のまま（全角の空白 width 字分）にそろえる。"""
    rest = max(0, int(width - form_generation.text_width(value)))
    fill = "\u3000" * rest
    return f"【{label}：{value}{fill}】"


# --- 生成と生成物 -----------------------------------------------------------------------


@transaction.atomic
def request_all(
    host: HostOrganization, fiscal_year: int, *, by: User | None
) -> list[GeneratedDocument]:
    """第3-6号・別紙2（受入れ機関）と別紙1（事業所ごと・5 人ごと）の生成を依頼する。

    基準日は年度末の 3月31日（支援の形態・担当者の基準日。ADR-0012）。
    """
    period = periodic_report.report_period(fiscal_year)
    lines = lines_of(host, fiscal_year)
    documents = [
        form_generation.request(
            REPORT_FORM_ID,
            target=host,
            data=build_report(host, fiscal_year, lines),
            basis_on=period.end,
            by=by,
        )
    ]
    for part, (site, sheet_lines) in enumerate(sheets_of(lines), 1):
        documents.append(
            form_generation.request(
                SHEET_FORM_ID,
                target=site,
                data=build_sheet(host, site, fiscal_year, sheet_lines, part),
                basis_on=period.end,
                by=by,
            )
        )
    documents.append(
        form_generation.request(
            SIGNATURE_FORM_ID,
            target=host,
            data=build_signature(host, fiscal_year),
            basis_on=period.end,
            by=by,
        )
    )
    return documents


def documents_of(host: HostOrganization, fiscal_year: int) -> list[GeneratedDocument]:
    """その受入れ機関・年度の定期届出の生成物（新しい順）。"""
    period = periodic_report.report_period(fiscal_year)
    host_type = ContentType.objects.get_for_model(HostOrganization)
    site_type = ContentType.objects.get_for_model(HostSite)
    sites = list(host.sites.values_list("pk", flat=True))
    return list(
        GeneratedDocument.objects.filter(template__form_id__in=FORM_IDS, basis_on=period.end)
        .filter(
            Q(content_type_id=host_type.pk, object_id=host.pk)
            | Q(content_type_id=site_type.pk, object_id__in=sites)
        )
        .select_related("template", "created_by")
        .order_by("-created_at", "-pk")
    )
