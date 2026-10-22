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
3. ダウンロード（PDF と記入済みの docx・xlsx）は can_download_pdf・can_download_filled を通した
   view からだけ（URL を作らない）。

生成履歴は上書きしない。同じ内容でも再生成は新しい行になる。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import tempfile
import unicodedata
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

from apps.contracts.models import EmploymentContract, EmploymentContractVersion
from apps.core.models import Action, AuditAction, User
from apps.forms_engine.models import FormMethod, FormTemplate, GeneratedDocument, GenerationStatus
from apps.hosts.models import CareerPlanVersion
from apps.support.models import Briefing, PeriodicInterview, SupportPlanVersion
from apps.workers.models import ForeignWorker
from services import (
    audit,
    authorization,
    form_checks,
    form_convert,
    form_docx,
    form_templates,
    form_translations,
    form_xlsx,
    jobs,
)
from services.authorization import AnyUser

SP = "\u3000"
FULL_WIDTH = str.maketrans("0123456789,.-%", "０１２３４５６７８９，．－％")
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
    # 全角の数字（Word の様式。T34-D6。記載例と同じ）
    if fmt == "zen":
        return str(_number(value)).translate(FULL_WIDTH)
    if fmt == "zen_2":
        return f"{int(value):02d}".translate(FULL_WIDTH)
    if fmt == "zen_yen":
        return f"{int(value):,}".translate(FULL_WIDTH)
    if fmt == "zen_text":
        return str(value).translate(FULL_WIDTH)
    raise MappingError(f"unknown format: {fmt}")


def text_width(text: str) -> float:
    """全角を 1、半角を 0.5 として数えた幅（欄の幅にそろえるとき）。"""
    return sum(0.5 if unicodedata.east_asian_width(char) in ("Na", "H") else 1 for char in text)


def padded(value: str, width: float, align: str) -> str:
    """欄の幅（全角の字数）に全角空白でそろえる（数字は右、文字は左に寄せる。T34-D6）。

    値が欄より長ければそのまま。半端な幅（0.5）は半角空白で埋める。
    """
    missing = width - text_width(value)
    if missing <= 0:
        return value
    fill = SP * int(missing) + (" " if missing - int(missing) >= 0.5 else "")
    return fill + value if align == "right" else value + fill


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


def _translated(item: Mapping[str, Any], raw: object) -> object:
    """欄の translate（言語）があれば値を訳の表で置き換える（無ければそのまま。ADR-0027）。"""
    language = item.get("translate")
    if not language or not isinstance(raw, str):
        return raw
    found = form_translations.translate_value(raw, form_translations.table(language))
    return raw if found is None else found


def field_value(item: Mapping[str, Any], data: Mapping[str, Any]) -> object | None:
    if item.get("format") == "template":
        return _template_value(item, data)
    raw = _translated(item, data.get(item["key"]))
    value = format_value(raw, item.get("format", "text"))
    if value is not None and "pad" in item:
        value = padded(str(value), float(item["pad"]), item.get("align", "left"))
    return value


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
    wrap: bool = False  # 欄の幅で改行する（T35-D11）


def pdf_merges(template: FormTemplate) -> list[str]:
    """PDF に変換する一時の写しでだけ結合する範囲（manifest の pdf_merges。ADR-0032）。"""
    return [entry["range"] for entry in (template.manifest or {}).get("pdf_merges", [])]


def merged_targets(template: FormTemplate) -> set[str]:
    """一時の結合の範囲の左上の書き込み先（字を小さくしない欄）。"""
    result = set()
    for merge in pdf_merges(template):
        sheet, area = form_xlsx.split_target(merge)
        result.add(f"{sheet}!{area.split(':')[0]}")
    return result


def text_areas(template: FormTemplate) -> dict[str, str]:
    """書き込み先 → 字を描く範囲（マッピングの fit_area と、一時の結合の範囲）。"""
    areas = {
        item["target"]: item["fit_area"]
        for item in template.mapping.definition.get("fields", [])
        if item.get("fit_area")
    }
    for merge in pdf_merges(template):
        sheet, area = form_xlsx.split_target(merge)
        areas[f"{sheet}!{area.split(':')[0]}"] = area
    return areas


def fits(template: FormTemplate, data: Mapping[str, Any]) -> list[Fit]:
    """fit の欄は 1 行に収まる字の大きさ（T31-D7）、wrap の欄は改行して結合範囲の高さに収まる
    字の大きさ（T35-D11）。wrap の欄は、値があれば字の大きさが元のままでも残す（改行を入れるため）。
    一時の結合の範囲の左上の欄（pdf_merges）は、字を小さくせず、収まらないときだけ知らせる
    （修正 1 回目 A6 の案 (e)、ADR-0032）。
    """
    definition = template.mapping.definition
    if template.method != FormMethod.XLSX_CELLS:
        return []
    areas = text_areas(template)
    fixed = {target for target in areas if target in merged_targets(template)}
    items = [
        item
        for item in definition.get("fields", [])
        if item.get("fit") or item.get("wrap") or item.get("target") in fixed
    ]
    values = {item["target"]: field_value(item, data) for item in items}
    wraps = {item["target"] for item in items if item.get("wrap")}
    targets = [target for target, value in values.items() if value not in (None, "")]
    if not targets:
        return []
    fonts = form_xlsx.cell_fonts(form_templates.template_file(template), targets, areas)
    minimum = float(definition.get("fit_min_size", DEFAULT_FIT_MIN_SIZE))
    result = []
    for target in targets:
        if target in wraps:
            size, overflow = form_xlsx.wrapped_size(str(values[target]), fonts[target], minimum)
            result.append(Fit(target, size, overflow, wrap=True))
            continue
        floor = fonts[target].size if target in fixed else minimum
        size, overflow = form_xlsx.fitted_size(str(values[target]), fonts[target], floor)
        if size < fonts[target].size or overflow:
            result.append(Fit(target, size, overflow))
    return result


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):  # 割増率・日数・時間など（書式 zen は文字のまま受ける）
        return str(value)
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- 依頼 ------------------------------------------------------------------------


def worker_of(target: models.Model) -> ForeignWorker | None:
    """生成の対象の外国人（外国人そのもの・ガイダンスの記録・計画の版・雇用契約の版）。"""
    if isinstance(target, ForeignWorker):
        return target
    if isinstance(target, Briefing):
        return target.worker
    if isinstance(target, SupportPlanVersion):
        return target.plan.worker
    if isinstance(target, EmploymentContractVersion):
        return target.contract.worker
    if isinstance(target, EmploymentContract):
        return target.worker
    if isinstance(target, PeriodicInterview):
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


def prepared(template: FormTemplate, data: Mapping[str, Any]) -> Mapping[str, Any]:
    """版に入れる値（訳文の版は値を訳の表で置き換える。T43、ADR-0035）。

    併記の版の中国語の行の欄（マッピングの translate）で訳の無い日本語の値は、日本語のまま入れ、
    欄の key を UNTRANSLATED に残す（生成前の検査の警告。ADR-0027 の追記）。
    """
    language = form_translations.language_of(template)
    if language:
        return form_translations.translate(data, language)
    fields = template.mapping.definition.get("fields", [])
    untranslated = sorted(
        {
            item["key"]
            for item in fields
            if item.get("translate")
            and isinstance(data.get(item["key"]), str)
            and form_translations.JAPANESE.search(data[item["key"]])
            and form_translations.translate_value(
                data[item["key"]], form_translations.table(item["translate"])
            )
            is None
        }
    )
    if not untranslated:
        return data
    return {**data, form_translations.UNTRANSLATED: untranslated}


def _check(
    template: FormTemplate, target: models.Model, data: Mapping[str, Any], basis_on: datetime.date
) -> form_checks.CheckResult:
    data = prepared(template, data)
    worker = worker_of(target)
    if worker is None:
        # 外国人が 1 人に決まらない帳票（第5-4号。T35）は、外国人を見る規則を持たない
        if template.mapping.rules:
            raise ValueError("the target has no foreign worker")
        return form_checks.CheckResult([], [])
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
    template: FormTemplate | None = None,
) -> GeneratedDocument:
    """生成を依頼する（検査を通し、生成履歴を作り、ジョブを登録する）。

    template は担当者が明示的に選んだ版（別の版で作り直す。T53-D2）。無ければ基準日で選ぶ。
    """
    if template is None:
        selection = form_templates.select_template(form_id, basis_on, language=language)
    else:
        if template.form_id != form_id:
            raise MappingError(f"the chosen version is not {form_id}")
        selection = form_templates.Selection(template, before_first_version=False)
    template = selection.template
    result = _check(template, target, data, basis_on)
    data = prepared(template, data)
    if result.blocking:
        raise GenerationBlocked(result)
    if result.warnings and not accept_warnings:
        raise WarningsNotAccepted(result)
    snapshot = {
        "form_id": form_id,
        "template_id": template.pk,
        "valid_from": template.valid_from.isoformat(),
        "language": template.language,  # 様式の版の言語（ja・ja-zh。T38）
        "basis_on": basis_on.isoformat(),
        "before_first_version": selection.before_first_version,
        "data": {key: _jsonable(value) for key, value in data.items()},
        # 長い値の字の大きさ（pt）と、下限でも収まらなかったか（T31-D7）
        "fitted": {
            fit.target: {"size": fit.size, "overflow": fit.overflow}
            | ({"wrap": True} if fit.wrap else {})
            for fit in fits(template, data)
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
        if form_id == "career-plan":
            # 生成に使ったキャリアアッププランの版は、これ以後は直せない（修正 1 回目 J1）
            from services import career_plans  # 循環を避ける

            used = data.get("p.plan_version_id")
            if isinstance(used, int):
                career_plans.mark_used(CareerPlanVersion.objects.filter(pk=used).first())
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
            areas = text_areas(template)
            fonts = form_xlsx.cell_fonts(source, [t for t in fitted if t in values], areas)
            for target, font in fonts.items():
                size = float(fitted[target]["size"])
                text = str(values[target])
                if fitted[target].get("wrap"):
                    text = "\n".join(form_xlsx.wrap_lines(text, font, size))
                values[target] = form_xlsx.SizedText(text, size, font.name, font.charset)
        form_xlsx.write_cells(source, filled, values)
        merges = pdf_merges(template)
        if merges:
            # 保存するブックは公式の構造のまま。変換の直前の一時の写しでだけ結合する（ADR-0032）
            copy = workdir / "for-pdf" / filled.name
            copy.parent.mkdir(exist_ok=True)
            form_xlsx.with_pdf_merges(filled, copy, merges)
            pdf = form_convert.convert(copy)
            copy.unlink()
            return filled, pdf
    else:
        filled = workdir / f"{template.form_id}.docx"
        form_docx.render(source, filled, values)
        supplement = (template.manifest or {}).get("supplement")
        if supplement:
            # 旧い併記の版に足りない事項の補足の頁（自作）を、同じ値で差し込んで末尾に綴じる
            # （ADR-0027 の追記）。保存する文書も補足の頁を含む（本人に渡す書面と同じ）
            extra = workdir / f"{template.form_id}-supplement.docx"
            form_docx.render(source.parent / supplement["file"], extra, values)
            form_docx.append_body(filled, extra)
            extra.unlink()
        # 保存する文書は公式の組み方のまま。変換の直前の一時の写しでだけ変える（ADR-0032）:
        # 頁の組み方（manifest の pdf_layout）と、中国語の字体の run の言語（ADR-0048）
        copy = workdir / "for-pdf" / filled.name
        source_for_pdf = filled
        layout = (template.manifest or {}).get("pdf_layout")
        if layout:
            form_docx.with_pdf_layout(filled, copy, layout)
            source_for_pdf = copy
        language = workdir / "for-pdf" / f"zh-{filled.name}"
        if form_docx.with_chinese_language(source_for_pdf, language).changed:
            language.replace(copy)
            source_for_pdf = copy
        else:
            language.unlink()
        if source_for_pdf != filled:
            pdf = form_convert.convert(copy)
            copy.unlink()
            return filled, pdf
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
        # 複数の外国人の個人情報を含む帳票（第5-4号）は、外国人を見る権限も要る（T35）
        return authorization.has_action(user, Action.WORKERS_VIEW)
    if not authorization.has_action(user, Action.WORKERS_VIEW):
        return False
    return authorization.scope(user, ForeignWorker.objects.filter(pk=document.worker_id)).exists()


def can_download_filled(user: AnyUser, document: GeneratedDocument) -> bool:
    """記入済みのファイル（編集用の docx・xlsx）をダウンロードできるか（D9・T53-D3）。

    生成物を見る権限と同じ（ダウンロードは監査ログに残す）。
    """
    if document.status != GenerationStatus.COMPLETED or not document.filled_file:
        return False
    return can_view(user, document)


def can_download_pdf(user: AnyUser, document: GeneratedDocument) -> bool:
    """生成物の PDF をダウンロードできるか（forms.generate と、対象の外国人を見る権限）。

    記入済みの docx・xlsx は画面から取り出せない（保存だけ。段階4 の F13 で決める）。
    """
    if document.status != GenerationStatus.COMPLETED or not document.pdf_file:
        return False
    return can_view(user, document)
