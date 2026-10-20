"""電子届出の一括届出（随時届出 6 種・特異事案報告）の行・検査・生成・提出の記録。

T50・T52。ADR-0040。

- 一括届出は、ツール 1 本（様式 ID）と届出機関（受入れ機関か自機関）ごと。行は届出タスク 1 件
  （雇用契約の変更は変更事項ごとに複数の行。T50-D10）。
- 行の値は services/efile_data（台帳と届出タスクからの既定値＋行の入力）、検査は
  services/efile_checks（ツールのマクロの入力チェックの写し）。
- 1 ファイルは様式の行数（20〜22 行）まで（T50-D3）。生成とダウンロードは監査ログに残す
  （値は残さない）。
  在留カード番号を含むので、ダウンロードは forms.generate と、行のすべての外国人を見られる人だけ。
- 提出の記録（申込の日・受付番号）は、行のすべての届出タスクに状態の変更として写す（T52-D2）。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.models import Action, AuditAction, User
from apps.filings.models import Filing, FilingStatus
from apps.forms_engine.models import (
    BulkFiling,
    BulkFilingFile,
    BulkFilingFiler,
    BulkFilingRow,
    FormTemplate,
)
from apps.hosts.models import HostOrganization
from apps.workers.models import ForeignWorker
from services import (
    audit,
    authorization,
    efile_checks,
    efile_data,
    filings,
    form_templates,
    form_xlsm,
)
from services.authorization import AnyUser

FORM_LABELS = {
    "efile-3-1": "第3-1号",
    "efile-3-2": "第3-2号",
    "efile-3-3": "第3-3号",
    "efile-3-4": "第3-4号",
    "efile-3-5": "第3-5号",
    "efile-3-7": "第3-7号",
    "efile-4-3": "第4-3号",
}
OPEN_FOR_ROWS = frozenset({FilingStatus.NOT_STARTED, FilingStatus.IN_PROGRESS})


@dataclass(frozen=True)
class Problem:
    """検査で見つかった 1 件（行ごと）。blocking でなければ警告。"""

    row: BulkFilingRow | None
    label: str
    message: str
    blocking: bool = True


def template_of(bulk: BulkFiling, on: datetime.date | None = None) -> FormTemplate:
    return form_templates.select_template(bulk.form_id, on or timezone.localdate()).template


def label_of(form_id: str) -> str:
    return FORM_LABELS.get(form_id, form_id)


def filer_of(form_id: str) -> BulkFilingFiler:
    return (
        BulkFilingFiler.ORGANIZATION
        if form_id in efile_data.ORGANIZATION_FORMS
        else BulkFilingFiler.HOST
    )


# --- 一括届出と行 ---------------------------------------------------------------------------


def create(
    form_id: str, host: HostOrganization | None, *, by: User | None, title: str = ""
) -> BulkFiling:
    if form_id not in efile_data.FORM_IDS:
        raise ValidationError(_("This form cannot be filed in bulk."))
    filer = filer_of(form_id)
    bulk = BulkFiling(
        form_id=form_id,
        filer=filer,
        host_organization=host if filer == BulkFilingFiler.HOST else None,
        title=title,
        created_by=by,
        updated_by=by,
    )
    if filer == BulkFilingFiler.HOST and host is None:
        raise ValidationError(_("Choose the host organization."))
    bulk.save()
    return bulk


def problems_to_add(bulk: BulkFiling, filing: Filing) -> str:
    """届出タスクをこの一括届出に入れられないわけ（入れられるなら空）。"""
    if bulk.submitted_on is not None:
        return _("The submission of this bulk notification has been recorded.")
    if efile_data.form_of(filing) != bulk.form_id:
        return _("The filing task is for another form.")
    if filing.worker_id is None:
        return _("The filing task has no foreign worker.")
    if FilingStatus(filing.status) not in OPEN_FOR_ROWS:
        return _("The filing task is not open.")
    if bulk.filer == BulkFilingFiler.HOST:
        host = efile_data.host_of(filing)
        if host is None or host.pk != bulk.host_organization_id:
            return _("The filing task is for another host organization.")
    if bulk.rows.filter(filing=filing).exists():
        return _("The filing task is already in this bulk notification.")
    return ""


def add_filing(bulk: BulkFiling, filing: Filing, *, by: User | None) -> list[BulkFilingRow]:
    """届出タスクの行を足す（既定値は届出タスクと台帳から）。"""
    reason = problems_to_add(bulk, filing)
    if reason:
        raise ValidationError(reason)
    assert filing.worker is not None
    table = template_of(bulk).mapping.definition["columns_table"]
    position = (bulk.rows.order_by("-position").values_list("position", flat=True).first() or 0) + 1
    rows = []
    for offset, values in enumerate(efile_data.defaults(filing, bulk.form_id, table)):
        row = BulkFilingRow(
            bulk=bulk,
            filing=filing,
            worker=filing.worker,
            position=position + offset,
            inputs=efile_data.jsonable(values),
            created_by=by,
            updated_by=by,
        )
        row.save()
        rows.append(row)
    return rows


def candidates(bulk: BulkFiling) -> list[Filing]:
    """この一括届出に入れられる届出タスク（未着手・対応中・同じツール・同じ届出機関）。"""
    kinds = [kind for kind, form_id in efile_data.KIND_FORMS.items() if form_id == bulk.form_id]
    tasks = (
        Filing.objects.filter(kind__in=kinds, status__in=list(OPEN_FOR_ROWS), worker__isnull=False)
        .exclude(bulk_rows__bulk=bulk)
        .select_related("worker")
        .order_by("unadjusted_due_on", "pk")
    )
    return [task for task in tasks if not problems_to_add(bulk, task)]


def open_bulk_for(filing: Filing, *, by: User | None) -> BulkFiling:
    """届出タスクを入れる一括届出（提出を記録していない同じツール・届出機関のもの。無ければ作る）。"""
    form_id = efile_data.form_of(filing)
    if not form_id:
        raise ValidationError(_("This filing task cannot be filed in bulk."))
    host = efile_data.host_of(filing) if filer_of(form_id) == BulkFilingFiler.HOST else None
    existing = BulkFiling.objects.filter(
        form_id=form_id, submitted_on__isnull=True, host_organization=host
    ).order_by("-pk")
    for bulk in existing:
        if bulk.rows.filter(filing=filing).exists() or not problems_to_add(bulk, filing):
            return bulk
    return create(form_id, host, by=by)


def save_inputs(row: BulkFilingRow, inputs: Mapping[str, Any], *, by: User | None) -> BulkFilingRow:
    kept = {key: value for key, value in (row.inputs or {}).items() if key == efile_data.ESTIMATED}
    row.inputs = kept | efile_data.jsonable(
        {key: value for key, value in inputs.items() if value not in (None, "")}
    )
    row.updated_by = by
    row.save(update_fields=["inputs", "updated_by", "updated_at"])
    return row


def remove_row(row: BulkFilingRow, *, by: User | None) -> None:
    if row.bulk.submitted_on is not None:
        raise ValidationError(_("The submission of this bulk notification has been recorded."))
    row.updated_by = by
    row.delete()  # 監査対象のモデル（削除はシグナルで残る）


def rows_of(bulk: BulkFiling) -> list[BulkFilingRow]:
    return list(
        bulk.rows.select_related("worker__nationality", "filing").order_by("position", "pk")
    )


# --- 値と検査 -------------------------------------------------------------------------------


def row_values(
    row: BulkFilingRow, definition: Mapping[str, Any], host: HostOrganization | None
) -> dict[str, Any]:
    table = definition["columns_table"]
    filing = row.filing
    contract = efile_data.contract_of(filing)
    values = efile_data.person_values(row.worker, contract, filing.basis_on, table)
    values |= {
        key: value for key, value in (row.inputs or {}).items() if key != efile_data.ESTIMATED
    }
    return values


def header_values(bulk: BulkFiling) -> dict[str, str]:
    return efile_data.organization_values(bulk.form_id, bulk.host_organization)


def advice(definition: Mapping[str, Any]) -> dict[str, int]:
    return {
        field["target"]: field["advice_length"]
        for field in definition["fields"]
        if field.get("advice_length")
    }


def built_rows(
    bulk: BulkFiling, template: FormTemplate
) -> tuple[list[BulkFilingRow], list[dict[str, str]]]:
    definition = template.mapping.definition
    rows = rows_of(bulk)
    cells = [
        efile_data.cells(definition, row_values(row, definition, bulk.host_organization))
        for row in rows
    ]
    return rows, cells


def check(bulk: BulkFiling, today: datetime.date | None = None) -> list[Problem]:
    """全部の行をツールのマクロの入力チェックの写しで確かめる（警告を含む）。"""
    today = today or timezone.localdate()
    template = template_of(bulk, today)
    definition = template.mapping.definition
    problems: list[Problem] = []
    header = header_values(bulk)
    for key, label in (("org.name", _("name of the filer")), ("org.address", _("address"))):
        if not header.get(key):
            problems.append(Problem(None, label, _("Enter it in the ledger.")))
    rows, cells = built_rows(bulk, template)
    for finding in efile_checks.check_rows(
        definition["columns_table"], cells, today, advice(definition)
    ):
        problems.append(Problem(rows[finding.row], finding.name, finding.message, finding.blocking))
    return problems


# --- 生成とダウンロード ----------------------------------------------------------------------


class GenerationBlocked(Exception):
    def __init__(self, problems: list[Problem]) -> None:
        super().__init__(f"{len(problems)} problem(s)")
        self.problems = problems


@transaction.atomic
def generate(
    bulk: BulkFiling, *, by: User | None, today: datetime.date | None = None
) -> list[BulkFilingFile]:
    """検査を通ったら、様式の行数ごとに xlsm を作る。生成不可の不足があれば GenerationBlocked。"""
    today = today or timezone.localdate()
    problems = [problem for problem in check(bulk, today) if problem.blocking]
    rows_all = rows_of(bulk)
    if problems or not rows_all:
        raise GenerationBlocked(problems)
    template = template_of(bulk, today)
    definition = template.mapping.definition
    table = definition["columns_table"]
    rows, cells = built_rows(bulk, template)
    header = efile_data.header_cells(definition, header_values(bulk))
    source = form_templates.templates_root() / template.original_path
    size = int(table["rows"])
    chunks = [
        list(zip(rows[start : start + size], cells[start : start + size], strict=True))
        for start in range(0, len(rows), size)
    ]
    created = []
    for part, chunk in enumerate(chunks, start=1):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "efile.xlsm"
            form_xlsm.write_rows(
                source,
                out,
                rows={table["sheet"]: [row_cells for _row, row_cells in chunk]},
                first_row=table["first_row"],
                formula_column=None,
                cells=header,
            )
            data = out.read_bytes()
        record = BulkFilingFile(
            bulk=bulk,
            template=template,
            part=part,
            parts=len(chunks),
            sha256=hashlib.sha256(data).hexdigest(),
            row_ids=[row.pk for row, _cells in chunk],
            worker_ids=sorted({row.worker_id for row, _cells in chunk}),
            snapshot=json.dumps(
                {
                    "header": header,
                    "rows": [{"row": row.pk, "cells": row_cells} for row, row_cells in chunk],
                },
                ensure_ascii=False,
            ),
            created_by=by,
            updated_by=by,
        )
        record.file.save(filename(bulk, part, len(chunks), today), ContentFile(data), save=False)
        with audit.event(
            AuditAction.GENERATE,
            target=bulk,
            actor=by,
            context={
                "form_id": template.form_id,
                "valid_from": str(template.valid_from),
                "part": part,
                "workers": record.worker_ids,
            },
        ):
            record.save()
        created.append(record)
    return created


def filename(bulk: BulkFiling, part: int, parts: int, on: datetime.date) -> str:
    suffix = f"_{part}of{parts}" if parts > 1 else ""
    return f"一括届出_{label_of(bulk.form_id)}_{on:%Y%m%d}_{bulk.pk}{suffix}.xlsm"


def can_view(user: AnyUser, bulk: BulkFiling) -> bool:
    return authorization.has_action(user, Action.FORMS_GENERATE) and authorization.has_action(
        user, Action.WORKERS_VIEW
    )


def can_download(user: AnyUser, record: BulkFilingFile) -> bool:
    """作った xlsm をダウンロードできるか（生成と同じ権限と、行のすべての外国人を見る権限）。"""
    if not can_view(user, record.bulk):
        return False
    visible = authorization.scope(user, ForeignWorker.objects.filter(pk__in=record.worker_ids))
    return visible.count() == len(set(record.worker_ids))


def record_download(record: BulkFilingFile, user: AnyUser) -> None:
    audit.record(
        AuditAction.DOWNLOAD,
        target=record.bulk,
        actor=user if isinstance(user, User) else None,
        context={"file": record.pk, "part": record.part, "workers": record.worker_ids},
    )


# --- 提出の記録（T52）-------------------------------------------------------------------------


def filings_of(bulk: BulkFiling) -> list[Filing]:
    seen: dict[int, Filing] = {}
    for row in rows_of(bulk):
        seen.setdefault(row.filing_id, row.filing)
    return list(seen.values())


@transaction.atomic
def record_submission(
    bulk: BulkFiling, *, submitted_on: datetime.date, receipt_number: str, by: User | None
) -> list[Filing]:
    """申込の日と受付番号を一括届出と、行のすべての届出タスク（未提出のもの）に記録する。"""
    if not bulk.files.exists():
        raise ValidationError(_("Create the Excel file first."))
    if submitted_on > timezone.localdate():
        raise ValidationError({"submitted_on": _("The date cannot be in the future.")})
    with audit.event(AuditAction.UPDATE, target=bulk, actor=by):
        bulk.submitted_on = submitted_on
        bulk.receipt_number = receipt_number
        bulk.updated_by = by
        bulk.save(update_fields=["submitted_on", "receipt_number", "updated_by", "updated_at"])
    changed = []
    for filing in filings_of(bulk):
        if FilingStatus(filing.status) not in OPEN_FOR_ROWS:
            continue
        filings.transition(
            filing,
            FilingStatus.SUBMITTED,
            by=by,
            submitted_on=submitted_on,
            receipt_number=receipt_number,
            context={"bulk_filing": bulk.pk},
        )
        changed.append(filing)
    return changed


def bulks_of_filing(filing: Filing) -> list[BulkFiling]:
    return list(BulkFiling.objects.filter(rows__filing=filing).distinct().order_by("-pk"))


def summary(rows: Sequence[BulkFilingRow]) -> dict[int, list[BulkFilingRow]]:
    """届出タスクごとの行（画面の表示）。"""
    result: dict[int, list[BulkFilingRow]] = {}
    for row in rows:
        result.setdefault(row.filing_id, []).append(row)
    return result
