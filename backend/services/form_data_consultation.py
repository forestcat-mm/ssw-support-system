"""相談記録書（参考様式第5-4号）のデータを、相談・苦情の記録（T20）から作る（T35、ADR-0024）。

- 単位は受入れ機関×期間（既定は 4 月 1 日〜翌年 3 月 31 日。T35-D9）。相談受理日（受付の記録の日）が
  期間の中で、その日の雇用契約がその受入れ機関のもの。取り消した相談は入れない。
- 様式の表は 9 行。10 件目からは同じ様式の 2 枚目として別の生成物にする（様式の注意「適宜２枚目」。
  T35-D10）。
- 機関の氏名又は名称は自機関（登録支援機関）の名称（T35-D9）。
- 書き方は記載例どおり: 日付は「2021/1/30」、性別は選んだ文字だけ、対応結果は経過ごとに日付と本文、
  通報・ハローワークへの相談は日付と機関の名称（記載例 ★２）。在留カード番号は受理日の時点のもの。
"""

from __future__ import annotations

import datetime
from typing import Any

from apps.core.models import Organization
from apps.hosts.models import HostOrganization
from apps.support.models import Consultation, ConsultationEntry
from apps.workers.models import ResidenceRecord, Sex
from services import contracts

ROWS = 9
SEX_WORDS: dict[str, str] = {Sex.MALE: "男", Sex.FEMALE: "女"}


def default_period(today: datetime.date) -> tuple[datetime.date, datetime.date]:
    """今日を含む年度（4 月 1 日〜翌年 3 月 31 日）。"""
    year = today.year if today.month >= 4 else today.year - 1
    return datetime.date(year, 4, 1), datetime.date(year + 1, 3, 31)


def consultations_for(
    host: HostOrganization, starts_on: datetime.date, ends_on: datetime.date
) -> list[Consultation]:
    rows = (
        Consultation.objects.filter(
            voided_at__isnull=True,
            record__voided_at__isnull=True,
            record__occurred_on__range=(starts_on, ends_on),
        )
        .select_related("record__staff", "worker__nationality")
        .order_by("record__occurred_on", "record__start_time", "pk")
    )
    result = []
    for consultation in rows:
        contract = contracts.contract_on(consultation.worker, consultation.record.occurred_on)
        if contract is not None and contract.host_organization_id == host.pk:
            result.append(consultation)
    return result


def pages(consultations: list[Consultation]) -> list[list[Consultation]]:
    return [consultations[i : i + ROWS] for i in range(0, len(consultations), ROWS)]


def slash_date(day: datetime.date | None) -> str:
    return f"{day.year}/{day.month}/{day.day}" if day is not None else ""


def card_number_on(consultation: Consultation) -> str:
    """受理日の時点の在留カード番号（その日以前に許可・交付された最新の行）。"""
    records = ResidenceRecord.objects.filter(
        worker=consultation.worker,
        voided_at__isnull=True,
        permitted_on__lte=consultation.record.occurred_on,
    ).order_by("-permitted_on", "-pk")
    # 番号は暗号化した列なので、空でない行を取り出してから選ぶ
    return next((record.card_number for record in records if record.card_number), "")


def entry_text(entry: ConsultationEntry) -> str:
    text = f"{slash_date(entry.occurred_on)} {' '.join(entry.body.split())}"
    if entry.reported:
        place = entry.get_report_to_display() if entry.report_to else ""
        if entry.report_to_note:
            place = f"{place}（{entry.report_to_note}）" if place else entry.report_to_note
        text += f"（{slash_date(entry.reported_on or entry.occurred_on)} {place}へ通報）"
    if entry.hello_work:
        text += f"（{slash_date(entry.hello_work_on or entry.occurred_on)} 公共職業安定所へ相談）"
    return text


def row_values(row: int, consultation: Consultation) -> dict[str, Any]:
    worker = consultation.worker
    record = consultation.record
    entries = list(consultation.entries.order_by("occurred_on", "start_time", "pk"))
    prefix = f"r{row}_"
    return {
        prefix + "received_on": slash_date(record.occurred_on),
        prefix + "name": worker.name_romaji,
        prefix + "sex": SEX_WORDS.get(worker.sex, ""),
        prefix + "nationality": worker.nationality.name if worker.nationality_id else "",
        prefix + "birth_date": slash_date(worker.birth_date),
        prefix + "card_number": card_number_on(consultation),
        prefix + "content": " ".join(record.body.split()),
        prefix + "result": "\n".join(entry_text(entry) for entry in entries),
        prefix + "staff": record.staff.name,
    }


def build(
    starts_on: datetime.date, ends_on: datetime.date, page: list[Consultation]
) -> dict[str, Any]:
    organization = Organization.objects.first()
    data: dict[str, Any] = {
        "from_m": starts_on.month,
        "from_d": starts_on.day,
        "to_m": ends_on.month,
        "to_d": ends_on.day,
        "organization_name": organization.name if organization is not None else "",
    }
    for row, consultation in enumerate(page, 1):
        data |= row_values(row, consultation)
    return {f"c.{key}": value for key, value in data.items()}
