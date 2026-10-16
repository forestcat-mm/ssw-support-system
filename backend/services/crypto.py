"""列暗号化とブラインドインデックス（第8章「在留カード番号・パスポート番号・口座情報は列暗号化」）。

- 暗号化: Fernet（AES-128-CBC + HMAC-SHA256）。鍵は FIELD_ENCRYPTION_KEYS（カンマ区切り）。
  先頭の鍵で暗号化し、すべての鍵で復号を試す（MultiFernet）。ローテーションは
  「新しい鍵を先頭に足す → manage.py rotate_encryption_keys → 古い鍵を外す」。
- ブラインドインデックス: 正規化した値の HMAC-SHA256。完全一致の検索だけに使う。
  鍵は暗号化とは別の BLIND_INDEX_KEY（在留カード番号は取りうる値が少なく、鍵なしの
  ハッシュでは総当たりで戻せるため）。鍵を変えたら manage.py rebuild_blind_indexes。
- 鍵を失うと復号できない。鍵は DB バックアップとは別の場所に保管する（本番準備）。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings

GENERATE_KEY_HINT = "make env を実行して .env に生成した鍵を書き込む"


class KeyConfigurationError(ValueError):
    """暗号化鍵・ブラインドインデックス鍵の設定が不正。"""


def encryption_keys() -> tuple[str, ...]:
    keys = tuple(key.strip() for key in settings.FIELD_ENCRYPTION_KEYS if key.strip())
    if not keys:
        raise KeyConfigurationError("FIELD_ENCRYPTION_KEYS is empty")
    return keys


@lru_cache(maxsize=8)
def _fernets(keys: tuple[str, ...]) -> tuple[MultiFernet, Fernet]:
    try:
        fernets = [Fernet(key.encode()) for key in keys]
    except (ValueError, binascii.Error) as error:
        raise KeyConfigurationError(f"invalid FIELD_ENCRYPTION_KEYS: {error}") from error
    return MultiFernet(fernets), fernets[0]


def _multi() -> MultiFernet:
    return _fernets(encryption_keys())[0]


def encrypt(plaintext: str) -> str:
    return _multi().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _multi().decrypt(token.encode()).decode()
    except InvalidToken as error:
        raise InvalidToken(
            "cannot decrypt with any of FIELD_ENCRYPTION_KEYS (key removed too early?)"
        ) from error


def is_encrypted_with_primary_key(token: str) -> bool:
    primary = _fernets(encryption_keys())[1]
    try:
        primary.decrypt(token.encode())
    except InvalidToken:
        return False
    return True


def reencrypt(token: str) -> str:
    """古い鍵の暗号文を、先頭（現在）の鍵で暗号化し直す。"""
    return _multi().rotate(token.encode()).decode()


def _blind_index_key() -> bytes:
    raw = str(settings.BLIND_INDEX_KEY)
    try:
        key = base64.urlsafe_b64decode(raw.encode())
    except (ValueError, binascii.Error) as error:
        raise KeyConfigurationError("BLIND_INDEX_KEY must be urlsafe base64") from error
    if len(key) < 32:
        raise KeyConfigurationError("BLIND_INDEX_KEY must decode to at least 32 bytes")
    return key


def normalize_for_index(value: str) -> str:
    """表記ゆれ（大小文字・空白・ハイフン・全角英数）を吸収する。"""
    halfwidth = value.translate(_FULLWIDTH_TO_HALFWIDTH)
    return re.sub(r"[\s\-‐]", "", halfwidth).upper()


def blind_index(value: str | None) -> str | None:
    """検索用の値。空なら None（検索対象にしない）。"""
    if not value:
        return None
    normalized = normalize_for_index(value)
    if not normalized:
        return None
    return hmac.new(_blind_index_key(), normalized.encode(), hashlib.sha256).hexdigest()


def check_configuration() -> list[str]:
    """鍵の設定の問題点（システムチェック用）。"""
    problems = []
    try:
        _fernets(encryption_keys())
    except KeyConfigurationError as error:
        problems.append(str(error))
    try:
        _blind_index_key()
    except KeyConfigurationError as error:
        problems.append(str(error))
    return problems


def generate_key() -> str:
    return Fernet.generate_key().decode()


_FULLWIDTH_TO_HALFWIDTH = str.maketrans(
    {chr(code): chr(code - 0xFEE0) for code in range(0xFF01, 0xFF5F)}
)
