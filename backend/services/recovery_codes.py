"""回復用コードの生成・正規化・ハッシュ（モデルに依存しない）。

形式は XXXX-XXXX-XXXX（12 文字、紛らわしい文字を除いた 32 文字種 = 60 ビット）。
十分な乱数長があるため、保存は SHA-256 のハッシュのみとし、照合はハッシュの一致で行う。
"""

import hashlib
import re
import secrets

ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # 0/1/I/O を除く 32 文字
CODE_LENGTH = 12
CODES_PER_SET = 10
_CODE_PATTERN = re.compile(rf"^[{ALPHABET}]{{{CODE_LENGTH}}}$")


def generate_codes(count: int = CODES_PER_SET) -> list[str]:
    return [
        _format("".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))) for _ in range(count)
    ]


def normalize(code: str) -> str | None:
    """入力を照合用の形（大文字・区切りなし）にする。形式外なら None。"""
    compact = re.sub(r"[\s-]", "", code).upper()
    return compact if _CODE_PATTERN.match(compact) else None


def hash_code(code: str) -> str:
    normalized = normalize(code)
    if normalized is None:
        raise ValueError("not a recovery code")
    return hashlib.sha256(normalized.encode()).hexdigest()


def looks_like_recovery_code(code: str) -> bool:
    return normalize(code) is not None


def _format(compact: str) -> str:
    return "-".join(compact[i : i + 4] for i in range(0, CODE_LENGTH, 4))
