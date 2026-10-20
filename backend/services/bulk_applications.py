"""在留申請オンラインの一括申請（認定・変更・更新）の行・検査・生成・ダウンロード（T48。ADR-0039）。

- 行（外国人ごと）の申請ごとの入力は inputs、犯罪を理由とする処分の有無と内容は暗号化の列に持つ。
  犯罪の値を見られるのは、その外国人の担当者と管理者（workers.sensitive_documents）だけ。
- 1 人分の値（services/bulk_application_data）を、欄の対応（mappings/bulk-*/<版>.json）でツールの
  表示名にし、ツールのマクロの入力チェックの写し（services/bulk_checks）で確かめてから、300 人ごとに
  xlsm を作る（services/form_xlsm）。
- 生成とダウンロードは監査ログに残す（値は残さない）。犯罪の申告が「有」の行を含むファイルは、
  管理者と、その行すべての外国人の担当者だけがダウンロードできる。スナップショットには犯罪の内容を残さない。
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils.translation import gettext as _

from apps.core.models import Action, AuditAction, User
from apps.forms_engine.models import (
    BULK_FORM_IDS,
    BulkApplication,
    BulkApplicationEntry,
    BulkApplicationFile,
    BulkProcedure,
    FormTemplate,
)
from apps.workers.models import ForeignWorker
from services import (
    audit,
    authorization,
    bulk_application_data,
    bulk_checks,
    form_templates,
    form_xlsm,
    immigration_codes,
    workers,
)
from services.authorization import AnyUser

PEOPLE_PER_FILE = 300  # 在留申請オンライン 操作マニュアル 53 頁
FULL_WIDTH_TYPES = frozenset(
    {"ひらがな", "全角カナ", "漢字", "全角英字", "全角数字", "全角記号", "全角スペース"}
)
HALF_WIDTH_TYPES = frozenset(
    {
        "半角数字",
        "半角英字",
        "半角英大文字",
        "半角記号",
        "半角ピリオド",
        "半角カンマ",
        "半角ハイフン",
    }
)
MASK = "***"


@dataclass(frozen=True)
class Problem:
    """検査で見つかった 1 件（外国人ごと）。"""

    entry: BulkApplicationEntry
    label: str
    message: str


def template_of(application: BulkApplication) -> FormTemplate:
    form_id = BULK_FORM_IDS[BulkProcedure(application.procedure)]
    return form_templates.select_template(form_id, application.basis_on).template


def procedure_key(application: BulkApplication) -> str:
    return bulk_application_data.PROCEDURE_KEYS[BulkProcedure(application.procedure)]


# --- 行と申請ごとの入力 ----------------------------------------------------------------------


def add_workers(
    application: BulkApplication, chosen: Iterable[ForeignWorker], *, by: User | None
) -> list[BulkApplicationEntry]:
    """外国人の行を足す（すでにある人は足さない）。入力は同じ外国人の前の行の答えを初期値にする。"""
    existing = set(application.entries.values_list("worker_id", flat=True))
    added = []
    for worker in chosen:
        if worker.pk in existing or not workers.is_active(worker):
            continue
        contract = bulk_application_data.contract_of(worker, application.basis_on)
        agent = (
            contract.host_organization.applicant_contacts.filter(is_active=True).first()
            if contract
            else None
        )
        entry = BulkApplicationEntry(
            application=application,
            worker=worker,
            agent=agent,
            inputs=bulk_application_data.default_inputs(worker),
            created_by=by,
            updated_by=by,
        )
        entry.save()
        added.append(entry)
    return added


def save_inputs(
    entry: BulkApplicationEntry,
    inputs: Mapping[str, Any],
    *,
    sensitive: Mapping[str, str] | None,
    by: User | None,
) -> BulkApplicationEntry:
    """申請ごとの入力を保存する。sensitive（犯罪の有無と内容）は None なら変えない。"""
    entry.inputs = {key: value for key, value in inputs.items() if value not in (None, "")}
    fields = ["inputs", "updated_by", "updated_at"]
    if sensitive is not None:
        crime = sensitive.get("crime", "")
        detail = sensitive.get("crime_detail", "").strip() if crime == "yes" else ""
        if crime == "yes" and not detail:
            raise ValidationError({"crime_detail": _("Enter the details of the criminal record.")})
        entry.sensitive = json.dumps({"crime": crime, "crime_detail": detail}, ensure_ascii=False)
        fields.append("sensitive")
    entry.updated_by = by
    entry.save(update_fields=fields)
    return entry


def is_assigned_staff(user: AnyUser, worker: ForeignWorker) -> bool:
    staff = worker.assigned_staff
    return (
        isinstance(user, User)
        and staff is not None
        and staff.user_id is not None
        and staff.user_id == user.pk
    )


def can_see_sensitive(user: AnyUser, worker: ForeignWorker) -> bool:
    """犯罪の申告を見られるか（要配慮添付と同じ。ADR-0038 決定 6）。"""
    return is_assigned_staff(user, worker) or authorization.has_action(
        user, Action.WORKERS_SENSITIVE_DOCUMENTS
    )


# --- 値と検査 -------------------------------------------------------------------------------


def _columns_by_cell(definition: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    table = definition["columns_table"]
    result: dict[str, dict[str, Any]] = {}
    for column in table["columns"]:
        result.setdefault(f"{column['sheet']}!{column['column']}", column)
    return result


def _text(value: str, column: Mapping[str, Any]) -> str:
    types = set(column.get("char_types") or [])
    if types and types <= FULL_WIDTH_TYPES:
        return bulk_application_data.to_full_width(value)
    if types and types <= HALF_WIDTH_TYPES:
        return unicodedata.normalize("NFKC", value)
    return value


def cell_values(
    definition: Mapping[str, Any], values: Mapping[str, str], procedure: str
) -> dict[str, dict[str, str]]:
    """1 人分の値を、シート → 列 → ツールに書く文字（コードは表示名）にする。"""
    by_cell = _columns_by_cell(definition)
    by_key = {item["key"]: item for item in definition["fields"]}
    result: dict[str, dict[str, str]] = {}
    for item in definition["fields"]:
        raw = str(values.get(item["key"], "") or "")
        if not raw:
            continue
        column = by_cell[item["target"]]
        if item["format"] == "code":
            text = ""
            parent = by_key.get(item.get("parent_key", ""))
            if parent is not None:
                parent_value = str(values.get(parent["key"], "") or "")
                rows = immigration_codes.procedure_linked(parent["code"], parent_value, procedure)
                text = next((label for code, label in rows or [] if code == raw), "")
            text = text or immigration_codes.full_label(item["code"], raw, procedure)
            text = text or raw  # 表に無いコードはそのまま（検査が知らせる）
        else:
            text = _text(raw, column)
        sheet, letters = item["target"].rsplit("!", 1)
        result.setdefault(sheet, {})[letters] = text
    return result


def entries_of(application: BulkApplication) -> list[BulkApplicationEntry]:
    return list(
        application.entries.select_related(
            "worker__nationality", "worker__assigned_staff", "agent__host_organization"
        ).order_by("pk")
    )


def rows_of(
    application: BulkApplication, entries: Sequence[BulkApplicationEntry]
) -> tuple[FormTemplate, list[dict[str, dict[str, str]]]]:
    template = template_of(application)
    definition = template.mapping.definition
    procedure = procedure_key(application)
    rows = [
        cell_values(definition, bulk_application_data.build(entry), procedure) for entry in entries
    ]
    return template, rows


def check(application: BulkApplication) -> list[Problem]:
    """全員の行を、ツールのマクロの入力チェックの写しで確かめる。"""
    entries = entries_of(application)
    if not entries:
        return []
    template, rows = rows_of(application, entries)
    definition = template.mapping.definition
    problems = []
    for entry in entries:
        if bulk_application_data.contract_of(entry.worker, application.basis_on) is None:
            problems.append(
                Problem(entry, "", _("There is no employment contract on the basis date."))
            )
    findings = bulk_checks.check_rows(definition["columns_table"], rows, procedure_key(application))
    for finding in findings:
        problems.append(Problem(entries[finding.person], finding.name, finding.message))
    return problems


# --- 生成とダウンロード ----------------------------------------------------------------------


def _chunks(items: Sequence[Any], size: int) -> list[Sequence[Any]]:
    return [items[start : start + size] for start in range(0, len(items), size)] or []


class GenerationBlocked(Exception):
    def __init__(self, problems: list[Problem]) -> None:
        super().__init__(f"{len(problems)} problem(s)")
        self.problems = problems


@transaction.atomic
def generate(application: BulkApplication, *, by: User | None) -> list[BulkApplicationFile]:
    """検査を通ったら、300 人ごとに xlsm を作る。検査で見つかれば GenerationBlocked。"""
    problems = check(application)
    if problems:
        raise GenerationBlocked(problems)
    entries = entries_of(application)
    if not entries:
        raise GenerationBlocked([])
    template, rows = rows_of(application, entries)
    definition = template.mapping.definition
    table = definition["columns_table"]
    sensitive_targets = {
        tuple(item["target"].rsplit("!", 1))
        for item in definition["fields"]
        if item["key"] in ("d.crime", "d.crime_detail")
    }
    source = form_templates.templates_root() / template.original_path
    pairs = list(zip(entries, rows, strict=True))
    chunks = _chunks(pairs, PEOPLE_PER_FILE)
    created = []
    for part, chunk in enumerate(chunks, start=1):
        sheets: dict[str, list[dict[str, str]]] = {sheet: [] for sheet in table["sheets"]}
        for _entry, row in chunk:
            for sheet in sheets:
                sheets[sheet].append(row.get(sheet, {}))
        sheets = {sheet: people for sheet, people in sheets.items() if any(people)}
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "bulk.xlsm"
            form_xlsm.write_rows(
                source,
                out,
                rows=sheets,
                first_row=table["first_row"],
                formula_column=table["row_number_column"],
                cells={f"{table['menu']['sheet']}!{table['menu']['cell']}": table["menu"]["value"]},
            )
            data = out.read_bytes()
        chunk_entries = [entry for entry, _row in chunk]
        includes_sensitive = any(bulk_application_data.has_crime(e) for e in chunk_entries)
        masked_rows = [
            {
                sheet: {
                    column: (MASK if (sheet, column) in sensitive_targets else value)
                    for column, value in cells.items()
                }
                for sheet, cells in row.items()
            }
            for _entry, row in chunk
        ]
        record = BulkApplicationFile(
            application=application,
            template=template,
            part=part,
            parts=len(chunks),
            sha256=hashlib.sha256(data).hexdigest(),
            worker_ids=[entry.worker_id for entry in chunk_entries],
            includes_sensitive=includes_sensitive,
            snapshot=json.dumps(
                [
                    {"worker": entry.worker_id, "values": values}
                    for entry, values in zip(chunk_entries, masked_rows, strict=True)
                ],
                ensure_ascii=False,
            ),
            created_by=by,
            updated_by=by,
        )
        name = filename(application, part, len(chunks))
        record.file.save(name, ContentFile(data), save=False)
        with audit.event(
            AuditAction.GENERATE,
            target=application,
            actor=by,
            context={
                "form_id": template.form_id,
                "valid_from": str(template.valid_from),
                "part": part,
                "workers": record.worker_ids,
                "includes_sensitive": includes_sensitive,
            },
        ):
            record.save()
        created.append(record)
    return created


def filename(application: BulkApplication, part: int, parts: int) -> str:
    label = {
        BulkProcedure.COE: "認定",
        BulkProcedure.EXTENSION: "更新",
        BulkProcedure.CHANGE: "変更",
    }[BulkProcedure(application.procedure)]
    suffix = f"_{part}of{parts}" if parts > 1 else ""
    return f"一括申請_{label}_{application.basis_on:%Y%m%d}_{application.pk}{suffix}.xlsm"


def can_download(user: AnyUser, record: BulkApplicationFile) -> bool:
    """作った xlsm をダウンロードできるか。

    生成と同じ権限。犯罪の申告を含めば、管理者と、その行すべての外国人の担当者だけ。
    """
    if not authorization.has_action(user, Action.FORMS_GENERATE):
        return False
    if not record.includes_sensitive:
        return True
    if authorization.has_action(user, Action.WORKERS_SENSITIVE_DOCUMENTS):
        return True
    entries = record.application.entries.filter(worker_id__in=record.worker_ids).select_related(
        "worker__assigned_staff"
    )
    flagged = [e.worker for e in entries if bulk_application_data.has_crime(e)]
    return bool(flagged) and all(is_assigned_staff(user, worker) for worker in flagged)


def record_download(record: BulkApplicationFile, user: AnyUser) -> None:
    audit.record(
        AuditAction.DOWNLOAD,
        target=record.application,
        actor=user if isinstance(user, User) else None,
        context={"file": record.pk, "part": record.part, "workers": record.worker_ids},
    )
