"""権限判定（第4章「権限設計の原則」）。判定はすべてこのモジュールに集約する。

- ロール×操作: RolePermission の行（データ）で決まる。
- 対象データの範囲: Role.data_scope ごとのルール（SCOPE_RULES）で一覧を絞る。
- Django admin の権限（"core.change_office" など）は DJANGO_PERMISSION_ACTIONS で操作に読み替える。
  対応表にない権限は拒否する（既定で拒否）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.contrib.auth.models import AnonymousUser
from django.db.models import Model, QuerySet

from apps.core.models import Action, DataScope, RolePermission, User

AnyUser = User | AnonymousUser

# データ範囲ルール: (ユーザー, 一覧) -> 見てよい行だけの一覧。
# ポータル追加時に DataScope.SELF（本人のみ）・HOST_ORGANIZATION（自社機関のみ）をここに足す。
SCOPE_RULES: dict[DataScope, Callable[[User, QuerySet[Any]], QuerySet[Any]]] = {
    DataScope.ALL: lambda user, queryset: queryset,
}


def _model_permissions(
    model: str,
    *,
    view: Action | None = None,
    add: Action | None = None,
    change: Action | None = None,
    delete: Action | None = None,
) -> dict[str, Action]:
    app_label, model_name = model.split(".")
    pairs = {"view": view, "add": add, "change": change, "delete": delete}
    return {
        f"{app_label}.{verb}_{model_name}": action
        for verb, action in pairs.items()
        if action is not None
    }


# Django の権限 -> 操作。削除は原則させない（無効化・履歴で表す）ため、必要なものだけ書く。
DJANGO_PERMISSION_ACTIONS: dict[str, Action] = {
    **_model_permissions(
        "core.user",
        view=Action.USERS_MANAGE,
        add=Action.USERS_MANAGE,
        change=Action.USERS_MANAGE,
    ),
    **_model_permissions(
        "core.role",
        view=Action.USERS_MANAGE,
        add=Action.USERS_MANAGE,
        change=Action.USERS_MANAGE,
    ),
    **_model_permissions(
        "core.rolepermission",
        view=Action.USERS_MANAGE,
        add=Action.USERS_MANAGE,
        change=Action.USERS_MANAGE,
        delete=Action.USERS_MANAGE,
    ),
    **_model_permissions("core.invitation", view=Action.USERS_MANAGE),
    **_model_permissions("core.auditlog", view=Action.AUDIT_VIEW),
    **_model_permissions(
        "filings.filing",
        view=Action.FILINGS_VIEW,
        add=Action.FILINGS_MANAGE,
        change=Action.FILINGS_MANAGE,
    ),
    # 支援実績・交付記録（添付のダウンロードの権限の判定に使う）
    **_model_permissions(
        "support.supportrecord",
        view=Action.SUPPORT_VIEW,
        add=Action.SUPPORT_MANAGE,
        change=Action.SUPPORT_MANAGE,
    ),
    **_model_permissions(
        "support.documentdelivery",
        view=Action.SUPPORT_VIEW,
        add=Action.SUPPORT_MANAGE,
        change=Action.SUPPORT_MANAGE,
    ),
    # 帳票（admin は開発者が様式マスタ・生成履歴を見るだけ。T29）
    **_model_permissions("forms_engine.formtemplate", view=Action.FORMS_GENERATE),
    **_model_permissions("forms_engine.formmapping", view=Action.FORMS_GENERATE),
    **_model_permissions("forms_engine.generateddocument", view=Action.FORMS_GENERATE),
    **_model_permissions("forms_engine.submissionbundle", view=Action.FORMS_GENERATE),
    # Django-Q2 の実行予定・結果（管理者のみ）
    **_model_permissions(
        "django_q.schedule",
        view=Action.SYSTEM_MANAGE,
        add=Action.SYSTEM_MANAGE,
        change=Action.SYSTEM_MANAGE,
        delete=Action.SYSTEM_MANAGE,
    ),
    **_model_permissions(
        "django_q.success", view=Action.SYSTEM_MANAGE, delete=Action.SYSTEM_MANAGE
    ),
    **_model_permissions(
        "django_q.failure", view=Action.SYSTEM_MANAGE, delete=Action.SYSTEM_MANAGE
    ),
    **_model_permissions(
        "core.userrole",
        view=Action.USERS_MANAGE,
        add=Action.USERS_MANAGE,
        change=Action.USERS_MANAGE,
        delete=Action.USERS_MANAGE,
    ),
    # ログイン試行の記録（django-axes）。ロック解除は AccessAttempt の削除で行う。
    **_model_permissions(
        "axes.accessattempt", view=Action.USERS_MANAGE, delete=Action.USERS_MANAGE
    ),
    **_model_permissions("axes.accesslog", view=Action.USERS_MANAGE),
    **_model_permissions("axes.accessfailurelog", view=Action.USERS_MANAGE),
    **{
        perm: action
        for model in (
            "workers.foreignworker",
            "workers.residencerecord",
            "workers.passportrecord",
            "workers.workeraddress",
            "workers.workercontact",
        )
        for perm, action in _model_permissions(
            model,
            view=Action.WORKERS_VIEW,
            add=Action.WORKERS_MANAGE,
            change=Action.WORKERS_MANAGE,
        ).items()
    },
    **{
        perm: action
        for model in (
            "contracts.employmentcontract",
            "contracts.employmentcontractversion",
            "contracts.employmentcontractshift",
            "contracts.employmentcontractallowance",
            "contracts.employmentcontractdeduction",
        )
        for perm, action in _model_permissions(
            model,
            view=Action.CONTRACTS_VIEW,
            add=Action.CONTRACTS_MANAGE,
            change=Action.CONTRACTS_MANAGE,
        ).items()
    },
    # 雇用条件のひな形（T41）: 見るのは契約を見る人、作る・直すのは受入れ機関を管理する人
    **_model_permissions(
        "contracts.employmentconditiontemplate",
        view=Action.CONTRACTS_VIEW,
        add=Action.HOSTS_MANAGE,
        change=Action.HOSTS_MANAGE,
    ),
    **{
        perm: action
        for model in (
            "core.country",
            "hosts.specifiedskillfield",
            "hosts.operationcategory",
        )
        for perm, action in _model_permissions(
            model,
            view=Action.ORGANIZATION_VIEW,
            add=Action.MASTERS_MANAGE,
            change=Action.MASTERS_MANAGE,
        ).items()
    },
    **{
        perm: action
        for model in (
            "hosts.hostorganization",
            "hosts.hostmembership",
            "hosts.hostcooperationconfirmation",
            "hosts.hostdocument",
            "hosts.hostsite",
            "hosts.supportcontract",
        )
        for perm, action in _model_permissions(
            model,
            view=Action.HOSTS_VIEW,
            add=Action.HOSTS_MANAGE,
            change=Action.HOSTS_MANAGE,
        ).items()
    },
    **{
        perm: action
        for model in (
            "core.organization",
            "core.office",
            "core.staff",
            "core.supportlanguage",
            "core.attachment",
        )
        for perm, action in _model_permissions(
            model,
            view=Action.ORGANIZATION_VIEW,
            add=Action.ORGANIZATION_MANAGE,
            change=Action.ORGANIZATION_MANAGE,
        ).items()
    },
}


def actions_of(user: AnyUser) -> frozenset[Action]:
    """ユーザーに許された操作。無効・未ログインは空。同じユーザーオブジェクトではキャッシュする。"""
    if not isinstance(user, User) or not user.is_active:
        return frozenset()
    cached: frozenset[Action] | None = getattr(user, "_authz_actions", None)
    if cached is None:
        cached = frozenset(
            Action(action)
            for action in RolePermission.objects.filter(role__user_roles__user=user)
            .values_list("action", flat=True)
            .distinct()
        )
        user._authz_actions = cached  # type: ignore[attr-defined]
    return cached


def has_action(user: AnyUser, action: Action) -> bool:
    return action in actions_of(user)


def scope[M: Model](user: AnyUser, queryset: QuerySet[M]) -> QuerySet[M]:
    """ユーザーのロールのデータ範囲で一覧を絞る。複数ロールは和集合。"""
    if not isinstance(user, User) or not user.is_active:
        return queryset.none()
    scopes = {
        DataScope(code) for code in user.user_roles.values_list("role__data_scope", flat=True)
    }
    if DataScope.ALL in scopes:
        return queryset
    result = queryset.none()
    for data_scope in scopes:
        result = result | SCOPE_RULES[data_scope](user, queryset)
    return result


def can(user: AnyUser, action: Action, obj: Model | None = None) -> bool:
    """操作が許され、かつ対象（指定時）がデータ範囲内か。"""
    if not has_action(user, action):
        return False
    if obj is None:
        return True
    return scope(user, type(obj)._default_manager.filter(pk=obj.pk)).exists()


def has_django_permission(user: AnyUser, perm: str, obj: Model | None = None) -> bool:
    action = DJANGO_PERMISSION_ACTIONS.get(perm)
    return action is not None and can(user, action, obj)


def has_django_module_permission(user: AnyUser, app_label: str) -> bool:
    prefix = f"{app_label}."
    return any(
        perm.startswith(prefix) and has_action(user, action)
        for perm, action in DJANGO_PERMISSION_ACTIONS.items()
    )


def django_permissions_of(user: AnyUser) -> set[str]:
    allowed = actions_of(user)
    return {perm for perm, action in DJANGO_PERMISSION_ACTIONS.items() if action in allowed}
