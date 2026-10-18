"""様式マスタの読み込みと版の選び方（仕様書 第6章「様式のバージョン管理」。T29、ADR-0018）。

ファイル（git で管理。旧版は消さない。制約5）:
- templates/forms/<様式ID>/<適用開始日>/manifest.json・original.*・template.docx（Word の様式だけ）
- mappings/<様式ID>/<適用開始日>.json（欄の対応 fields）
- mappings/<様式ID>/<適用開始日>.rules.json（生成前の検査の規則。T30、ADR-0019）

読み込み（load_form_templates）は何度実行しても同じ結果になる。新しい版は足すだけで旧版を消さない。
- original の SHA-256 は manifest と一致し、読み込み済みの版から変わってはならない（止める）。
- template とマッピング定義は、その版で生成履歴がまだ無いときだけ置き換える（開発中の調整のため）。
  生成履歴がある版を変えるときは、新しい版（適用開始日）として足す。
- 規則のファイルは、生成履歴があっても置き換えてよい（出力を変えないため）。SHA-256 を記録し、
  変わったら FormMapping の更新として監査ログに残る。生成履歴には、検査に使った規則の SHA-256 を
  残す。

版の選び方: 基準日以前に始まる最新の版。どの版の適用開始日より前でも警告は出さず、最も古い版を使う。
改正日の分からない版（revision_unknown）は、ADR に記録した最も早い取得日を適用開始日にしている。
"""

from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils.translation import gettext as _

from apps.core.models import User
from apps.forms_engine.models import FormMapping, FormMethod, FormOwner, FormTemplate
from services import form_checks, form_xlsx

MANIFEST = "manifest.json"
REQUIRED_KEYS = ("form_id", "name", "valid_from", "language", "owner", "method", "original")


class FormTemplateError(Exception):
    """様式のファイルの不備（読み込みを止める）。"""


@dataclass
class LoadReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    rules_updated: list[str] = field(default_factory=list)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def templates_root() -> Path:
    return Path(settings.FORM_TEMPLATES_ROOT)


def mappings_root() -> Path:
    return Path(settings.FORM_MAPPINGS_ROOT)


def template_file(template: FormTemplate) -> Path:
    """生成に使うファイル（Word は template.docx、Excel は original）。"""
    relative = template.template_path or template.original_path
    return templates_root() / relative


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FormTemplateError(f"{path}: {error}") from error
    missing = [key for key in REQUIRED_KEYS if key not in data]
    if missing:
        raise FormTemplateError(f"{path}: missing {', '.join(missing)}")
    if data["method"] not in FormMethod.values:
        raise FormTemplateError(f"{path}: unknown method {data['method']}")
    if data["owner"] not in FormOwner.values:
        raise FormTemplateError(f"{path}: unknown owner {data['owner']}")
    if path.parent.name != data["valid_from"] or path.parent.parent.name != data["form_id"]:
        raise FormTemplateError(f"{path}: folder does not match form_id / valid_from")
    if data["method"] == FormMethod.DOCX_TAGS and not data.get("template"):
        raise FormTemplateError(f"{path}: a Word form needs template.docx")
    return data


def _checked_sha(folder: Path, name: str, expected: str | None, label: str) -> str:
    path = folder / name
    if not path.is_file():
        raise FormTemplateError(f"{path}: not found")
    actual = sha256_of(path)
    if expected and expected != actual:
        raise FormTemplateError(f"{path}: {label} SHA-256 does not match the manifest")
    return actual


@transaction.atomic
def load_all(*, by: User | None = None) -> LoadReport:
    report = LoadReport()
    root = templates_root()
    for manifest_path in sorted(root.glob(f"*/*/{MANIFEST}")):
        _load_one(manifest_path, by=by, report=report)
    return report


def _load_one(manifest_path: Path, *, by: User | None, report: LoadReport) -> None:
    data = _read_manifest(manifest_path)
    folder = manifest_path.parent
    source = data.get("source") or {}
    original_sha = _checked_sha(folder, data["original"], source.get("original_sha256"), "original")
    template_sha = (
        _checked_sha(folder, data["template"], source.get("template_sha256"), "template")
        if data.get("template")
        else ""
    )
    mapping_path = mappings_root() / data["form_id"] / f"{data['valid_from']}.json"
    if not mapping_path.is_file():
        raise FormTemplateError(f"{mapping_path}: not found")
    mapping_sha = sha256_of(mapping_path)
    try:
        definition = json.loads(mapping_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise FormTemplateError(f"{mapping_path}: {error}") from error

    if definition.get("rules"):
        raise FormTemplateError(
            f"{mapping_path}: rules belong in the rules file ({mapping_path.stem}.rules.json)"
        )
    rules_path = mapping_path.with_name(f"{data['valid_from']}.rules.json")
    rules, rules_sha = _read_rules(rules_path, data, definition)
    if data["method"] == FormMethod.XLSX_CELLS:
        _check_xlsx_targets(
            folder / (data.get("template") or data["original"]), definition, mapping_path
        )
    valid_from = datetime.date.fromisoformat(data["valid_from"])
    relative = folder.relative_to(templates_root())
    values: dict[str, Any] = {
        "name": data["name"],
        "method": data["method"],
        "requires_signed_copy": bool(data.get("requires_signed_copy")),
        "source_url": source.get("url", ""),
        "source_page": source.get("page", ""),
        "retrieved_on": datetime.date.fromisoformat(source["retrieved_on"]),
        "revision_date": (
            datetime.date.fromisoformat(source["revision_date"])
            if source.get("revision_date")
            else None
        ),
        "revision_unknown": bool(source.get("revision_date_unknown")),
        "original_path": str(relative / data["original"]),
        "original_sha256": original_sha,
        "template_path": str(relative / data["template"]) if data.get("template") else "",
        "template_sha256": template_sha,
        "manifest": data,
    }
    key = f"{data['form_id']} {data['valid_from']} {data['language']}"
    template = FormTemplate.objects.filter(
        form_id=data["form_id"],
        valid_from=valid_from,
        language=data["language"],
        owner=data["owner"],
        host_organization__isnull=True,
    ).first()
    if template is None:
        template = FormTemplate(
            form_id=data["form_id"],
            valid_from=valid_from,
            language=data["language"],
            owner=data["owner"],
            created_by=by,
            updated_by=by,
            **values,
        )
        template.save()
        FormMapping.objects.create(
            template=template,
            path=str(mapping_path.relative_to(mappings_root())),
            sha256=mapping_sha,
            definition=definition,
            rules_path=str(rules_path.relative_to(mappings_root())),
            rules_sha256=rules_sha,
            rules=rules,
            created_by=by,
            updated_by=by,
        )
        report.created.append(key)
        return

    if template.original_sha256 != original_sha:
        raise FormTemplateError(
            _(
                "%(key)s: the original file changed. Add it as a new version (valid-from date) "
                "instead of replacing it."
            )
            % {"key": key}
        )
    mapping = template.mapping
    changed = template.template_sha256 != template_sha or mapping.sha256 != mapping_sha
    rules_changed = mapping.rules_sha256 != rules_sha
    if not changed and template.manifest == data and not rules_changed:
        report.unchanged.append(key)
        return
    if rules_changed:
        mapping.rules_path = str(rules_path.relative_to(mappings_root()))
        mapping.rules_sha256 = rules_sha
        mapping.rules = rules
        report.rules_updated.append(key)
    if not changed and template.manifest == data:
        mapping.updated_by = by
        mapping.save()
        return
    if template.generated_documents.exists() and changed:
        raise FormTemplateError(
            _(
                "%(key)s: documents were generated with this version. Add the changed template "
                "or mapping as a new version."
            )
            % {"key": key}
        )
    for name, value in values.items():
        setattr(template, name, value)
    template.updated_by = by
    template.save()
    mapping.sha256 = mapping_sha
    mapping.definition = definition
    mapping.updated_by = by
    mapping.save()
    report.updated.append(key)


def _read_rules(
    path: Path, manifest: dict[str, Any], definition: dict[str, Any]
) -> tuple[list[dict[str, Any]], str]:
    """規則のファイルを読み、評価できる形かを確かめる（規則の key はその版の fields にある）。"""
    if not path.is_file():
        raise FormTemplateError(f"{path}: not found")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise FormTemplateError(f"{path}: {error}") from error
    if not isinstance(data, dict):
        raise FormTemplateError(f"{path}: must be an object")
    for name in ("form_id", "valid_from"):
        if data.get(name) != manifest[name]:
            raise FormTemplateError(f"{path}: {name} does not match the manifest")
    keys = {item["key"] for item in definition.get("fields", [])}
    try:
        form_checks.validate_rules(data.get("rules"), keys)
    except form_checks.RulesError as error:
        raise FormTemplateError(f"{path}: {error}") from error
    return data["rules"], sha256_of(path)


def _check_xlsx_targets(workbook: Path, definition: dict[str, Any], mapping_path: Path) -> None:
    """Excel の書き込み先が書けるセルで、ブックの計算式から参照されていないこと（ADR-0018）。

    値を書いたセルを参照する計算式があると、PDF に古い計算結果（キャッシュ）が出るおそれがある。
    その様式を扱うときは、変換のときの再計算を入れてから、この検査を外す。
    """
    targets = [item["target"] for item in definition.get("fields", [])]
    try:
        form_xlsx.check_targets(workbook, targets)
    except form_xlsx.XlsxWriteError as error:
        raise FormTemplateError(f"{mapping_path}: {error}") from error
    referenced = form_xlsx.targets_referenced_by_formulas(workbook, targets)
    if referenced:
        raise FormTemplateError(
            f"{mapping_path}: cells referenced by formulas: {', '.join(referenced)}"
        )


@dataclass(frozen=True)
class Selection:
    template: FormTemplate
    before_first_version: bool  # 基準日がどの版の適用開始日より前（最も古い版を使った）


def select_template(form_id: str, basis_on: datetime.date, *, language: str = "ja") -> Selection:
    """基準日に適用される共通の版（無ければ最も古い版）。"""
    versions = FormTemplate.objects.filter(
        form_id=form_id, language=language, owner=FormOwner.COMMON
    ).order_by("valid_from")
    applicable = versions.filter(valid_from__lte=basis_on).last()
    if applicable is not None:
        return Selection(applicable, before_first_version=False)
    oldest = versions.first()
    if oldest is None:
        raise FormTemplateError(
            _("The form %(form_id)s is not loaded. Run load_form_templates.") % {"form_id": form_id}
        )
    return Selection(oldest, before_first_version=True)
