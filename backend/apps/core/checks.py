"""起動時のシステムチェック。"""

from typing import Any

from django.core.checks import Error, register

from services import crypto


@register()
def check_encryption_keys(app_configs: Any = None, **kwargs: Any) -> list[Error]:
    """暗号化鍵・ブラインドインデックス鍵が正しい形式か（ダミーのままなら起動させない）。"""
    return [
        Error(problem, hint=crypto.GENERATE_KEY_HINT, id="core.E001")
        for problem in crypto.check_configuration()
    ]
