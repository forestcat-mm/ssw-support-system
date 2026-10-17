"""監査ログ（第8章）。記録の判断・差分・マスクはすべてこのモジュールに集約する。

- 作成・更新・削除: AUDITED_MODELS のモデルをシグナルで記録する
  （admin・サービス・シェルのどこから保存しても）。
  bulk_create / QuerySet.update() はシグナルを通らないため、使う側で event() に含める。
- 閲覧: VIEW_AUDITED_MODELS の詳細表示で record_view() を呼ぶ（admin は TrackedModelAdmin が呼ぶ）。
- 意味のあるイベント（無効化・MFA リセット・招待など）: event() で 1 件にまとめる。中の保存は
  一般の記録を止め、対象の変更前後の値をイベントの changes に入れる。
- 平文を残さない: MASKED_FIELDS と、列の型の audit_mask = True（T06 の暗号化列）は "***" にする。
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models.fields.files import FieldFile
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_save
from django.http import HttpRequest

from apps.core.models import AuditAction, AuditLog, User
from services.client_ip import client_ip

MASK = "***"

# 作成・更新・削除を記録するモデル（app_label.model_name）
AUDITED_MODELS: frozenset[str] = frozenset(
    {
        "core.user",
        "core.role",
        "core.rolepermission",
        "core.userrole",
        "core.organization",
        "core.office",
        "core.staff",
        "core.supportlanguage",
        "core.attachment",
        "core.invitation",
        "core.country",
        "workers.foreignworker",
        "workers.residencerecord",
        "workers.passportrecord",
        "workers.workeraddress",
        "workers.workercontact",
        "hosts.specifiedskillfield",
        "hosts.operationcategory",
        "hosts.hostorganization",
        "hosts.hostmembership",
        "hosts.hostcooperationconfirmation",
        "hosts.hostsite",
        "hosts.supportcontract",
        # 雇用契約（報酬を含む）
        "contracts.employmentcontract",
        "contracts.employmentcontractversion",
        "contracts.employmentcontractshift",
        "contracts.employmentcontractallowance",
        "contracts.employmentcontractdeduction",
        "filings.filing",
        # 支援計画（版とその中身は作成だけ。変更・削除はモデルが拒否する）
        "support.supportplan",
        "support.supportplanversion",
        "support.supportplanitem",
        "support.supportplanactivity",
        "support.supportplanhousing",
        "support.supportplanconsultation",
        "support.supportplanmunicipalitycheck",
        "support.supportrecord",
        "support.periodicinterview",
        "support.consultation",
        "support.consultationentry",
        "support.briefing",
        "support.briefingsession",
        "support.briefingcheck",
        "support.briefingforeignfee",
    }
)

# 詳細の閲覧も記録するモデル（個人情報を含むもの）。外国人の履歴は外国人の詳細画面に
# インラインで出るため、外国人の閲覧として 1 件記録される
# （履歴を単独で表示する画面を作るときも記録される）
VIEW_AUDITED_MODELS: frozenset[str] = frozenset(
    {
        "core.user",
        "core.staff",
        "core.organization",
        "workers.foreignworker",
        "workers.residencerecord",
        "workers.passportrecord",
        "workers.workeraddress",
        "workers.workercontact",
        # 受入れ機関は代表者の氏名を含む。加入・協力確認書・事業所・契約は受入れ機関の詳細として扱う
        "hosts.hostorganization",
        "hosts.hostmembership",
        "hosts.hostcooperationconfirmation",
        "hosts.hostsite",
        "hosts.supportcontract",
        # 雇用契約（報酬を含む）
        "contracts.employmentcontract",
        "contracts.employmentcontractversion",
        "contracts.employmentcontractshift",
        "contracts.employmentcontractallowance",
        "contracts.employmentcontractdeduction",
        "filings.filing",
    }
)

# apps.* のうち記録しないモデルと理由（記録漏れ検出テストで全モデルの分類を確認する）
EXCLUDED_MODELS: Mapping[str, str] = {
    "core.auditlog": "監査ログ自身",
    "core.recoverycodedevice": "MFA 端末（ADR-0001）。設定・リセットはイベントで記録",
    "core.recoverycode": "MFA 端末（ADR-0001）。ハッシュのみで内容は記録しない",
    "seed.seedobject": "ダミーデータの台帳（開発用。seed_dummy が書く）",
    "workers.latestresidencerecord": "最新行の DB ビュー（読み取り専用）",
    "workers.latestpassportrecord": "最新行の DB ビュー（読み取り専用）",
    "workers.latestworkeraddress": "最新行の DB ビュー（読み取り専用）",
    "workers.latestworkercontact": "最新行の DB ビュー（読み取り専用）",
}

# 値を記録しない列（変わったことだけ "***" で残す）
MASKED_FIELDS: frozenset[str] = frozenset(
    {
        "core.user.password",
        "core.invitation.token_hash",
    }
)

# 差分に含めない列（毎回変わる・操作者と重複するもの）
IGNORED_FIELDS: frozenset[str] = frozenset(
    {"created_at", "updated_at", "created_by", "updated_by", "last_login"}
)


@dataclass
class _Context:
    request: HttpRequest | None = None
    suppressed: int = 0
    pending_m2m: dict[tuple[int, str], list[str]] = field(default_factory=dict)


_context: ContextVar[_Context | None] = ContextVar("audit_context", default=None)


def _current() -> _Context:
    ctx = _context.get()
    if ctx is None:
        ctx = _Context()
        _context.set(ctx)
    return ctx


@contextmanager
def bind_request(request: HttpRequest) -> Iterator[None]:
    """リクエスト中の記録に操作者・IP・パスを付ける（AuditContextMiddleware から使う）。"""
    token = _context.set(_Context(request=request))
    try:
        yield
    finally:
        _context.reset(token)


@contextmanager
def suppressed() -> Iterator[None]:
    """中の保存をシグナルで記録しない（event() の中で使う）。"""
    ctx = _current()
    ctx.suppressed += 1
    try:
        yield
    finally:
        ctx.suppressed -= 1


def label_of(model: type[models.Model] | models.Model) -> str:
    return model._meta.label_lower


def is_audited(model: type[models.Model] | models.Model) -> bool:
    return label_of(model) in AUDITED_MODELS


def is_view_audited(model: type[models.Model] | models.Model) -> bool:
    return label_of(model) in VIEW_AUDITED_MODELS


# --- 記録 ------------------------------------------------------------------


_UNSET: Any = object()


def record(
    action: AuditAction,
    *,
    target: models.Model | None = None,
    changes: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
    actor: User | None = _UNSET,
) -> AuditLog:
    """監査ログを 1 件追加する。actor を省略するとリクエストのログインユーザーになる。"""
    request = _current().request
    if actor is _UNSET:
        actor = _request_user(request)
    entry = AuditLog(
        action=action,
        actor=actor,
        actor_username=actor.get_username() if actor is not None else "",
        changes=dict(changes or {}),
        context=dict(context or {}),
        ip_address=client_ip(request) if request is not None else None,
        path=request.path[:255] if request is not None else "",
    )
    if target is not None:
        entry.content_type = ContentType.objects.get_for_model(target, for_concrete_model=False)
        entry.object_id = str(target.pk)
        entry.object_repr = str(target)[:255]
    entry.save()
    return entry


def record_view(target: models.Model, *, context: Mapping[str, Any] | None = None) -> None:
    if is_view_audited(target):
        record(AuditAction.VIEW, target=target, context=context)


def record_list_view(
    screen: str, *, conditions: Mapping[str, Any], object_ids: list[Any]
) -> AuditLog:
    """一覧の閲覧の記録（1 回の表示で 1 件）。

    context に画面・条件・件数・表示した対象の ID を入れる。

    検索語に数字があれば番号（在留カード番号・旅券番号）の検索とみなしてマスクする。
    """
    safe: dict[str, Any] = {}
    for key, value in conditions.items():
        if value in (None, ""):
            continue
        if isinstance(value, models.Model):
            value = value.pk
        elif key == "q" and any(character.isdigit() for character in str(value)):
            value = MASK
        safe[key] = value if isinstance(value, (str, int, bool)) else str(value)
    return record(
        AuditAction.VIEW,
        context={
            "screen": screen,
            "conditions": safe,
            "count": len(object_ids),
            "object_ids": list(object_ids),
        },
    )


def record_export(
    *, target: models.Model | None = None, context: Mapping[str, Any] | None = None
) -> AuditLog:
    """エクスポート（CSV 等）の記録。context に件数・条件・形式を入れる。"""
    return record(AuditAction.EXPORT, target=target, context=context)


@dataclass
class Event:
    """event() の中で補足情報を足すための入れ物。"""

    context: dict[str, Any] = field(default_factory=dict)
    changes: dict[str, Any] = field(default_factory=dict)


@contextmanager
def event(
    action: AuditAction,
    *,
    target: models.Model,
    actor: User | None = _UNSET,
    context: Mapping[str, Any] | None = None,
) -> Iterator[Event]:
    """意味のある操作を 1 件で記録する。対象の変更前後の値（マスク済み）を changes に入れる。

    中の保存は一般の作成・更新の記録を止める。例外が起きたら記録しない。
    """
    # request.user（SimpleLazyObject）でも実体のモデルを取れるよう _meta.model を使う
    model = target._meta.model
    old = model._base_manager.filter(pk=target.pk).first() if target.pk else None
    before = snapshot(old) if old is not None else {}
    holder = Event(context=dict(context or {}))
    with suppressed():
        yield holder
    changes = diff(before, snapshot(target)) | masked_changes(old, target) | holder.changes
    record(action, target=target, changes=changes, context=holder.context, actor=actor)


# --- 差分・マスク ------------------------------------------------------------


def snapshot(instance: models.Model) -> dict[str, Any]:
    """具象列の値を JSON にできる形で返す（マスク・除外済み）。外部キーは主キー。"""
    values: dict[str, Any] = {}
    for model_field in instance._meta.concrete_fields:
        if model_field.name in IGNORED_FIELDS:
            continue
        values[model_field.name] = (
            MASK if _is_masked(instance, model_field) else _jsonable(model_field, instance)
        )
    return values


def diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, list[Any]]:
    """{列名: [変更前, 変更後]}（値が変わった列だけ）。

    マスク列は両方 "***" になり比べられないため、呼び出し側が masked_changes() で補う。
    """
    return {
        name: [before.get(name), after.get(name)]
        for name in sorted(after.keys() | before.keys())
        if before.get(name) != after.get(name)
    }


def masked_changes(old: models.Model | None, new: models.Model) -> dict[str, list[str]]:
    """マスク列のうち実際の値が変わったもの。値は残さず ["***", "***"] にする。"""
    if old is None:
        return {}
    return {
        model_field.name: [MASK, MASK]
        for model_field in new._meta.concrete_fields
        if _is_masked(new, model_field)
        and model_field.value_from_object(old) != model_field.value_from_object(new)
    }


def _is_masked(instance: models.Model, model_field: models.Field[Any, Any]) -> bool:
    return (
        f"{label_of(instance)}.{model_field.name}" in MASKED_FIELDS
        or getattr(model_field, "audit_mask", False) is True
    )


def _jsonable(model_field: models.Field[Any, Any], instance: models.Model) -> Any:
    value = model_field.value_from_object(instance)
    if isinstance(value, FieldFile):
        return value.name or ""
    if isinstance(value, datetime.datetime | datetime.date | datetime.time):
        return value.isoformat()
    if isinstance(value, decimal.Decimal | uuid.UUID):
        return str(value)
    return value


def _request_user(request: HttpRequest | None) -> User | None:
    user = getattr(request, "user", None)
    return user if isinstance(user, User) else None


# --- シグナル ------------------------------------------------------------------


def _should_record(sender: type[models.Model], raw: bool = False) -> bool:
    # マイグレーション中の歴史モデル（__fake__）と fixture 読み込み（raw）は記録しない
    return (
        not raw
        and sender.__module__ != "__fake__"
        and is_audited(sender)
        and _current().suppressed == 0
    )


def _on_pre_save(sender: type[models.Model], instance: models.Model, **kwargs: Any) -> None:
    if not _should_record(sender, kwargs.get("raw", False)) or instance.pk is None:
        return
    # 比較は post_save で行う（BlindIndexField などは pre_save シグナルの後に値が決まるため）
    instance._audit_old = sender._base_manager.filter(pk=instance.pk).first()  # type: ignore[attr-defined]


def _on_post_save(
    sender: type[models.Model], instance: models.Model, created: bool, **kwargs: Any
) -> None:
    if not _should_record(sender, kwargs.get("raw", False)):
        return
    after = snapshot(instance)
    if created:
        record(
            AuditAction.CREATE, target=instance, changes={k: [None, v] for k, v in after.items()}
        )
        return
    old: models.Model | None = getattr(instance, "_audit_old", None)
    before = snapshot(old) if old is not None else {}
    changes = diff(before, after) | masked_changes(old, instance)
    if changes:
        record(AuditAction.UPDATE, target=instance, changes=changes)


def _on_post_delete(sender: type[models.Model], instance: models.Model, **kwargs: Any) -> None:
    if not _should_record(sender):
        return
    record(
        AuditAction.DELETE,
        target=instance,
        changes={k: [v, None] for k, v in snapshot(instance).items()},
    )


def _on_m2m_changed(
    sender: type[models.Model],
    instance: models.Model,
    action: str,
    reverse: bool,
    **kwargs: Any,
) -> None:
    if reverse or not _should_record(type(instance)):
        return
    m2m_field = next(
        (f for f in instance._meta.many_to_many if f.remote_field.through is sender), None
    )
    if m2m_field is None:
        return
    key = (id(instance), m2m_field.name)
    current = sorted(str(obj) for obj in getattr(instance, m2m_field.name).all())
    pending = _current().pending_m2m
    if action.startswith("pre_"):
        pending[key] = current
    elif action.startswith("post_"):
        before = pending.pop(key, current)
        if before != current:
            record(AuditAction.UPDATE, target=instance, changes={m2m_field.name: [before, current]})


def connect_signals() -> None:
    """CoreConfig.ready() から呼ぶ。

    対象かどうかはハンドラ内で判定する（T07 以降のアプリのモデルも同じ仕組みで記録される）。
    """
    pre_save.connect(_on_pre_save, dispatch_uid="audit_pre_save")
    post_save.connect(_on_post_save, dispatch_uid="audit_post_save")
    post_delete.connect(_on_post_delete, dispatch_uid="audit_post_delete")
    m2m_changed.connect(_on_m2m_changed, dispatch_uid="audit_m2m_changed")
