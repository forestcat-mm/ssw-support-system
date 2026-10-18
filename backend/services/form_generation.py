"""帳票の生成と生成履歴（仕様書 第6章「生成履歴」、第5章 F08・F12。T29、ADR-0018）。

流れ:
0. check(): 基準日に適用される版の規則で生成前の検査をする（T30、ADR-0019）。生成不可があれば
   request() は GenerationBlocked で止め、生成履歴を作らない。警告は accept_warnings=True で確かめた
   ときだけ生成し、生成履歴に警告（findings）と規則のファイルの SHA-256 を残す。
1. request(): 基準日に適用される版を選び、データのスナップショット（暗号化）を持つ生成履歴を
   「依頼」で作り、ジョブ（Django-Q2）を登録する。
2. run()（ジョブ）: マッピング定義でデータを様式の欄の値にし、Excel はセルの値だけを書き、Word は
   差込タグに差し込み、PDF に変換する。完了なら記入済みのファイルと PDF を保存する。失敗なら出力を
   残さず、失敗の理由（個人情報を含まない）を残す。
3. ダウンロード（PDF だけ）は can_download_pdf を通した view からだけ（URL を作らない）。

生成履歴は上書きしない。同じ内容でも再生成は新しい行になる。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.files import File
from django.db import models, transaction
from django.utils import timezone
from pypdf import PdfReader

from apps.core.models import Action, AuditAction, User
from apps.forms_engine.models import FormMethod, FormTemplate, GeneratedDocument, GenerationStatus
from apps.support.models import Briefing
from apps.workers.models import ForeignWorker
from services import (
    audit,
    authorization,
    form_checks,
    form_convert,
    form_docx,
    form_templates,
    form_xlsx,
    jobs,
)
from services.authorization import AnyUser

CHECKED = "☑"
UNCHECKED = "□"


class MappingError(Exception):
    """マッピング定義の不備（欄の書式が分からない など）。"""


class GenerationBlocked(Exception):
    """生成不可の不足がある（生成履歴を作らない）。"""

    def __init__(self, result: form_checks.CheckResult) -> None:
        super().__init__(f"{len(result.blocking)} blocking finding(s)")
        self.result = result


class WarningsNotAccepted(Exception):
    """警告を確かめずに生成しようとした（accept_warnings=True が要る）。"""

    def __init__(self, result: form_checks.CheckResult) -> None:
        super().__init__(f"{len(result.warnings)} warning(s) not accepted")
        self.result = result


# --- 値の書式 ---------------------------------------------------------------------


def format_value(value: Any, fmt: str) -> object:
    """データの値を様式の欄の値にする（書式はマッピング定義の format）。"""
    if value is None or value == "":
        return ""
    if fmt == "text":
        return str(value)
    if fmt == "number":
        return int(value)
    if fmt == "yen":
        return f"{int(value):,}"
    if fmt in ("year", "month", "day"):
        day = value if isinstance(value, datetime.date) else datetime.date.fromisoformat(value)
        return {"year": day.year, "month": day.month, "day": day.day}[fmt]
    if fmt == "date":
        day = value if isinstance(value, datetime.date) else datetime.date.fromisoformat(value)
        return f"{day.year}年{day.month}月{day.day}日"
    if fmt == "check":
        return CHECKED if value else UNCHECKED
    raise MappingError(f"unknown format: {fmt}")


def field_values(definition: Mapping[str, Any], data: Mapping[str, Any]) -> dict[str, object]:
    """マッピング定義の欄ごとの値（キーは Excel ならセル、Word ならタグの名前）。"""
    values: dict[str, object] = {}
    for item in definition.get("fields", []):
        values[item["target"]] = format_value(data.get(item["key"]), item.get("format", "text"))
    return values


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- 依頼 ------------------------------------------------------------------------


def worker_of(target: models.Model) -> ForeignWorker | None:
    """生成の対象の外国人（対象が外国人そのもの、またはガイダンスの記録）。"""
    if isinstance(target, ForeignWorker):
        return target
    if isinstance(target, Briefing):
        return target.worker
    return None


def check(
    form_id: str,
    *,
    target: models.Model,
    data: Mapping[str, Any],
    basis_on: datetime.date,
    language: str = "ja",
) -> form_checks.CheckResult:
    """生成前の検査（基準日に適用される版の規則）。生成の画面（T31〜）と request() が使う。"""
    template = form_templates.select_template(form_id, basis_on, language=language).template
    return _check(template, target, data, basis_on)


def _check(
    template: FormTemplate, target: models.Model, data: Mapping[str, Any], basis_on: datetime.date
) -> form_checks.CheckResult:
    worker = worker_of(target)
    if worker is None:
        raise ValueError("the target has no foreign worker")
    context = form_checks.CheckContext(worker=worker, target=target, basis_on=basis_on, data=data)
    return form_checks.evaluate(template.mapping.rules, context)


def request(
    form_id: str,
    *,
    target: models.Model,
    data: Mapping[str, Any],
    basis_on: datetime.date,
    by: User | None,
    language: str = "ja",
    accept_warnings: bool = False,
) -> GeneratedDocument:
    """生成を依頼する（検査を通し、生成履歴を作り、ジョブを登録する）。"""
    selection = form_templates.select_template(form_id, basis_on, language=language)
    template = selection.template
    result = _check(template, target, data, basis_on)
    if result.blocking:
        raise GenerationBlocked(result)
    if result.warnings and not accept_warnings:
        raise WarningsNotAccepted(result)
    snapshot = {
        "form_id": form_id,
        "template_id": template.pk,
        "valid_from": template.valid_from.isoformat(),
        "basis_on": basis_on.isoformat(),
        "before_first_version": selection.before_first_version,
        "data": {key: _jsonable(value) for key, value in data.items()},
    }
    with transaction.atomic():
        document = GeneratedDocument.objects.create(
            template=template,
            target=target,
            worker=worker_of(target),
            basis_on=basis_on,
            revision_unknown=template.revision_unknown,
            rules_sha256=template.mapping.rules_sha256,
            findings=[finding.as_json() for finding in result.warnings],
            snapshot=json.dumps(snapshot, ensure_ascii=False),
            created_by=by,
            updated_by=by,
        )
    if settings.JOBS_INLINE:
        jobs.enqueue("services.form_generation.run", document.pk)  # テストではその場で
    else:
        # ワーカーがコミット前の行を読まないように、コミットの後に登録する
        transaction.on_commit(lambda: jobs.enqueue("services.form_generation.run", document.pk))
    document.refresh_from_db()
    return document


# --- 生成（ジョブ）-----------------------------------------------------------------


def run(document_id: int) -> None:
    """生成のジョブ。完了・失敗のどちらでも生成履歴を閉じる（上書きしない）。"""
    document = GeneratedDocument.objects.select_related("template", "template__mapping").get(
        pk=document_id
    )
    if document.status != GenerationStatus.REQUESTED:
        return
    snapshot = json.loads(document.snapshot)
    try:
        with tempfile.TemporaryDirectory(prefix="form-") as workdir:
            filled, pdf = _render(document.template, snapshot["data"], Path(workdir))
            _complete(document, filled, pdf)
    except Exception as error:  # noqa: BLE001  失敗は理由を残して閉じる（出力は残さない）
        _fail(document, error)


def _render(template: FormTemplate, data: Mapping[str, Any], workdir: Path) -> tuple[Path, Path]:
    source = form_templates.template_file(template)
    values = field_values(template.mapping.definition, data)
    if template.method == FormMethod.XLSX_CELLS:
        filled = workdir / f"{template.form_id}.xlsx"
        form_xlsx.write_cells(source, filled, values)
    else:
        filled = workdir / f"{template.form_id}.docx"
        form_docx.render(source, filled, values)
    return filled, form_convert.convert(filled)


def _complete(document: GeneratedDocument, filled: Path, pdf: Path) -> None:
    actor = document.created_by
    pages = len(PdfReader(str(pdf)).pages)
    with audit.event(
        AuditAction.GENERATE,
        target=document,
        actor=actor,
        context={
            "form_id": document.template.form_id,
            "valid_from": document.template.valid_from.isoformat(),
            "pages": pages,
        },
    ):
        stem = f"{document.template.form_id}-{document.pk}"
        with filled.open("rb") as handle:
            document.filled_file.save(f"{stem}{filled.suffix}", File(handle), save=False)
        with pdf.open("rb") as handle:
            document.pdf_file.save(f"{stem}.pdf", File(handle), save=False)
        document.filled_sha256 = _sha256(filled)
        document.pdf_sha256 = _sha256(pdf)
        document.page_count = pages
        document.converter_version = form_convert.converter_version()
        document.generated_at = timezone.now()
        document.status = GenerationStatus.COMPLETED
        document.save()


def failure_reason(error: Exception) -> str:
    """失敗の理由（個人情報を含まない）。自前の例外は文言を、それ以外は例外の種類だけを残す。"""
    if isinstance(
        error,
        (
            form_convert.ConversionError,
            form_xlsx.XlsxWriteError,
            MappingError,
            form_templates.FormTemplateError,
        ),
    ):
        return f"{type(error).__name__}: {error}"[:200]
    return type(error).__name__[:200]


def _fail(document: GeneratedDocument, error: Exception) -> None:
    with audit.event(
        AuditAction.GENERATE,
        target=document,
        actor=document.created_by,
        context={"form_id": document.template.form_id, "failed": True},
    ):
        document.status = GenerationStatus.FAILED
        document.failure_reason = failure_reason(error)
        document.save()


# --- ダウンロード -------------------------------------------------------------------


def can_download_pdf(user: AnyUser, document: GeneratedDocument) -> bool:
    """生成物の PDF をダウンロードできるか（forms.generate と、対象の外国人を見る権限）。

    記入済みの docx・xlsx は画面から取り出せない（保存だけ。段階4 の F13 で決める）。
    """
    if not authorization.has_action(user, Action.FORMS_GENERATE):
        return False
    if document.status != GenerationStatus.COMPLETED or not document.pdf_file:
        return False
    if document.worker_id is not None:
        if not authorization.has_action(user, Action.WORKERS_VIEW):
            return False
        visible = authorization.scope(user, ForeignWorker.objects.filter(pk=document.worker_id))
        return visible.exists()
    return True
