"""起動時のシステムチェック。"""

from typing import Any

from django.conf import settings
from django.core.checks import Error, register

from services import crypto


@register()
def check_encryption_keys(app_configs: Any = None, **kwargs: Any) -> list[Error]:
    """暗号化鍵・ブラインドインデックス鍵が正しい形式か（ダミーのままなら起動させない）。"""
    return [
        Error(problem, hint=crypto.GENERATE_KEY_HINT, id="core.E001")
        for problem in crypto.check_configuration()
    ]


# 添付の保存先の設定で、ファイルに直接の URL で届く設定（ADR-0011）
PUBLIC_ACLS = frozenset({"public-read", "public-read-write", "authenticated-read"})


def storage_problems(backend: str, options: dict[str, Any]) -> list[str]:
    """保存先の設定のうち、認可つきのダウンロードを経ずにファイルへ届く設定。"""
    problems = []
    if backend.endswith("S3Storage"):
        acl = options.get("default_acl")
        if acl in PUBLIC_ACLS:
            problems.append(f"default_acl={acl} makes uploaded files public.")
        if options.get("custom_domain"):
            problems.append("custom_domain serves files without authorization.")
        if options.get("querystring_auth") is False:
            problems.append("querystring_auth=false makes file URLs work without a signature.")
    return problems


@register()
def check_private_storage(app_configs: Any = None, **kwargs: Any) -> list[Error]:
    """添付の保存先が非公開の設定か（公開 ACL・独自ドメイン・署名なしの URL を拒否する）。"""
    default = settings.STORAGES["default"]
    return [
        Error(
            problem,
            hint="Keep the bucket private. See docs/decisions/0011-sensitive-attachments.md.",
            id="core.E002",
        )
        for problem in storage_problems(default["BACKEND"], default.get("OPTIONS", {}))
    ]
