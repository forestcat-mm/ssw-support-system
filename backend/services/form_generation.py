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
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.files import File
from django.db import models, transaction
from django.utils import timezone
from pypdf import PdfReader

from apps.core.models import Action, AuditAction, User
from apps.forms_engine.models import FormMethod, FormTemplate, GeneratedDocument, GenerationStatus
from apps.support.models import Briefing, SupportPlanVersion
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

CHECKED = "■"  # 置き換え先のフォント（IPAP明朝）にある記号（T31-D6）
UNCHECKED = "□"
DEFAULT_FIT_MIN_SIZE = 6.0  # 長い値の字を小さくするときの下限（pt。T31-D7）


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


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value == "")


def _number(value: Any) -> object:
    number = Decimal(str(value))
    if number == number.to_integral_value():
        return int(number)
    return f"{number.normalize():f}"


def format_value(value: Any, fmt: str) -> object | None:
    """データの値を様式の欄の値にする（書式はマッピング定義の format）。

    空の値（None・""）は None を返し、その欄には書かない（印刷済みの文字や □ を残す。T31-D4）。
    check は True を ■、False を □ にする。
    """
    if fmt == "check":
        return None if value is None else (CHECKED if value else UNCHECKED)
    if _empty(value):
        return None
    if fmt == "text":
        return str(value)
    if fmt == "number":
        return _number(value)
    if fmt == "yen":
        return f"{int(value):,}"
    if fmt in ("year", "month", "day"):
        day = value if isinstance(value, datetime.date) else datetime.date.fromisoformat(value)
        return {"year": day.year, "month": day.month, "day": day.day}[fmt]
    if fmt == "date":
        day = value if isinstance(value, datetime.date) else datetime.date.fromisoformat(value)
        return f"{day.year}年{day.month}月{day.day}日"
    raise MappingError(f"unknown format: {fmt}")


def _template_value(item: Mapping[str, Any], data: Mapping[str, Any]) -> str | None:
    """template の書式: 印刷済みの文に値を差し込んだ文（T31-D5）。

    keys の値がすべて空なら書かない。空の値は、その key の empty（無ければ ""）にする。
    値があれば、key の pattern（例 "　{}　"）があればそれで包む。
    """
    parts: dict[str, object] = {}
    filled = False
    for name, spec in item["keys"].items():
        value = format_value(data.get(spec["key"]), spec.get("format", "text"))
        if value is None:
            parts[name] = spec.get("empty", "")
        else:
            parts[name] = spec["pattern"].format(value) if "pattern" in spec else value
            filled = True
    if not filled:
        return None
    return str(item["template"]).format(**parts)


def field_keys(item: Mapping[str, Any]) -> list[str]:
    """欄が使うデータの key（template は keys のすべて）。"""
    if item.get("format") == "template":
        return [spec["key"] for spec in item["keys"].values()]
    return [item["key"]]


def field_value(item: Mapping[str, Any], data: Mapping[str, Any]) -> object | None:
    if item.get("format") == "template":
        return _template_value(item, data)
    return format_value(data.get(item["key"]), item.get("format", "text"))


def field_values(definition: Mapping[str, Any], data: Mapping[str, Any]) -> dict[str, object]:
    """マッピング定義の欄ごとの値（キーは Excel ならセル、Word ならタグの名前）。空の欄は除く。"""
    values: dict[str, object] = {}
    for item in definition.get("fields", []):
        value = field_value(item, data)
        if value is not None:
            values[item["target"]] = value
    return values


@dataclass(frozen=True)
class Fit:
    """長い値の字の大きさ（スナップショットに残す）。"""

    target: str
    size: float
    overflow: bool  # 下限の大きさでも収まらない


def fits(template: FormTemplate, data: Mapping[str, Any]) -> list[Fit]:
    """fit を指定した Excel の欄で、値が 1 行に収まる字の大きさ（T31-D7）。"""
    definition = template.mapping.definition
    if template.method != FormMethod.XLSX_CELLS:
        return []
    items = [item for item in definition.get("fields", []) if item.get("fit")]
    values = {item["target"]: field_value(item, data) for item in items}
    targets = [target for target, value in values.items() if value is not None]
    if not targets:
        return []
    fonts = form_xlsx.cell_fonts(form_templates.template_file(template), targets)
    minimum = float(definition.get("fit_min_size", DEFAULT_FIT_MIN_SIZE))
    result = []
    for target in targets:
        size, overflow = form_xlsx.fitted_size(str(values[target]), fonts[target], minimum)
        if size < fonts[target].size or overflow:
            result.append(Fit(target, size, overflow))
    return result


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
    if isinstance(target, SupportPlanVersion):
        return target.plan.worker
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
    context = form_checks.CheckContext(
        worker=worker, target=target, basis_on=basis_on, data=data, template=template
    )
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
        # 長い値の字の大きさ（pt）と、下限でも収まらなかったか（T31-D7）
        "fitted": {
            fit.target: {"size": fit.size, "overflow": fit.overflow} for fit in fits(template, data)
        },
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
    # 行をロックして、同じ生成履歴を 2 つのジョブが同時に作らないようにする（T33）。
    # ほかのジョブが処理中（ロック中）なら何もしない。
    with transaction.atomic():
        document = (
            GeneratedDocument.objects.select_for_update(skip_locked=True, of=("self",))
            .select_related("template", "template__mapping")
            .filter(pk=document_id)
            .first()
        )
        if document is None or document.status != GenerationStatus.REQUESTED:
            return
        snapshot = json.loads(document.snapshot)
        try:
            # 失敗が DB の誤りでも、失敗の記録を残せるように、セーブポイントの中で生成する
            with transaction.atomic(), tempfile.TemporaryDirectory(prefix="form-") as workdir:
                filled, pdf = _render(
                    document.template, snapshot["data"], Path(workdir), snapshot.get("fitted", {})
                )
                _complete(document, filled, pdf)
        except Exception as error:  # noqa: BLE001  失敗は理由を残して閉じる（出力は残さない）
            document.refresh_from_db()
            _fail(document, error)


def _render(
    template: FormTemplate,
    data: Mapping[str, Any],
    workdir: Path,
    fitted: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[Path, Path]:
    source = form_templates.template_file(template)
    values = field_values(template.mapping.definition, data)
    if template.method == FormMethod.XLSX_CELLS:
        filled = workdir / f"{template.form_id}.xlsx"
        if fitted:
            fonts = form_xlsx.cell_fonts(source, [t for t in fitted if t in values])
            for target, font in fonts.items():
                values[target] = form_xlsx.SizedText(
                    str(values[target]), float(fitted[target]["size"]), font.name, font.charset
                )
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


def is_stale(document: GeneratedDocument, now: datetime.datetime | None = None) -> bool:
    """依頼のまま、設定の時間（FORM_STALE_AFTER 秒）を過ぎた（qcluster の停止など。T33-D5）。"""
    if document.status != GenerationStatus.REQUESTED:
        return False
    now = now or timezone.now()
    return (now - document.created_at).total_seconds() > settings.FORM_STALE_AFTER


def can_view(user: AnyUser, document: GeneratedDocument) -> bool:
    """生成物の画面を見られるか（forms.generate と、対象の外国人を見る権限）。"""
    if not authorization.has_action(user, Action.FORMS_GENERATE):
        return False
    if document.worker_id is None:
        return True
    if not authorization.has_action(user, Action.WORKERS_VIEW):
        return False
    return authorization.scope(user, ForeignWorker.objects.filter(pk=document.worker_id)).exists()


def can_download_pdf(user: AnyUser, document: GeneratedDocument) -> bool:
    """生成物の PDF をダウンロードできるか（forms.generate と、対象の外国人を見る権限）。

    記入済みの docx・xlsx は画面から取り出せない（保存だけ。段階4 の F13 で決める）。
    """
    if document.status != GenerationStatus.COMPLETED or not document.pdf_file:
        return False
    return can_view(user, document)
