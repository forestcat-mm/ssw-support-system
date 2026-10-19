"""入力の正規化（修正 1 回目 B13）。保存のときにフォームで使い、入力中は forms.js が同じ規則で直す。

種類（data-normalize の値）:
- corporate_number: 法人番号（全角→半角、空白・ハイフンを外す）
- residence_card: 在留カード番号（全角→半角、空白・ハイフンを外す、大文字）
- passport: 旅券番号（全角→半角、空白・ハイフンを外す、大文字）
- postal: 郵便番号（全角→半角、〒・空白・ハイフンを外し、7 桁なら 000-0000 にする）
- phone: 電話番号（全角→半角、ハイフンの仲間を「-」にそろえる、前後の空白を外す）
- email: メール（全角→半角、前後の空白を外す）
- kana: カナ（半角カナ→全角、ひらがな→カタカナ。workers.to_katakana）
- amount: 金額（全角→半角、カンマ・空白・円・¥ を外す）

形式の検査（桁数・検査用数字など）は今までどおり各サービスで行う。ここでは直すだけ。
"""

from __future__ import annotations

import re
import unicodedata

from services import corporate_number, passport, residence_card

KINDS = (
    "corporate_number",
    "residence_card",
    "passport",
    "postal",
    "phone",
    "email",
    "kana",
    "amount",
)
_DASHES = re.compile(r"[‐‑‒–—―−ー－]")


def normalize(kind: str, value: str) -> str:
    if kind not in KINDS:
        raise ValueError(kind)
    if kind == "corporate_number":
        return corporate_number.normalize(value.strip())
    if kind == "residence_card":
        return residence_card.normalize(value.strip())
    if kind == "passport":
        return passport.normalize(value.strip())
    if kind == "kana":
        from services import workers  # 循環を避ける（workers は多くのサービスを読む）

        return workers.to_katakana(value)
    text = unicodedata.normalize("NFKC", value).strip()
    if kind == "postal":
        digits = re.sub(r"[\s〒\-]", "", _DASHES.sub("-", text))
        return f"{digits[:3]}-{digits[3:]}" if re.fullmatch(r"[0-9]{7}", digits) else text
    if kind == "phone":
        return _DASHES.sub("-", text)
    if kind == "amount":
        return re.sub(r"[\s,円¥￥]", "", text)
    return text  # email
