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
- date: 日付（「20260710」「2026/7/10」「2026年7月10日」「R8.7.10」「令和8年7月10日」→ 2026-07-10。
  修正 1 回目 B16）
- time: 時刻（「930」「0930」「9:30」「9時30分」→ 09:30。「9」「9時」→ 09:00。修正 1 回目 B16）

形式の検査（桁数・検査用数字など）は今までどおり各サービスで行う。ここでは直すだけ。
"""

from __future__ import annotations

import datetime
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
    "date",
    "time",
)
# 和暦の元年の西暦（年は「元」も受ける）
ERAS = {"令和": 2019, "r": 2019, "平成": 1989, "h": 1989, "昭和": 1926, "s": 1926}
_WESTERN = re.compile(r"^(\d{4})[/\-.年](\d{1,2})[/\-.月](\d{1,2})日?$")
_COMPACT = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_ERA = re.compile(r"^(令和|平成|昭和|[rhs])\.?(\d{1,2}|元)[/\-.年](\d{1,2})[/\-.月](\d{1,2})日?$")
_TIME = re.compile(r"^(\d{1,2})(?:[:.時](\d{1,2})分?|時)?$")
_TIME_COMPACT = re.compile(r"^(\d{1,2})(\d{2})$")


def _date(text: str) -> str:
    value = re.sub(r"\s+", "", text).lower()
    year = month = day = 0
    if match := _COMPACT.match(value) or _WESTERN.match(value):
        year, month, day = (int(part) for part in match.groups())
    elif match := _ERA.match(value):
        era, number, month_text, day_text = match.groups()
        year = ERAS[era] + (1 if number == "元" else int(number)) - 1
        month, day = int(month_text), int(day_text)
    else:
        return text
    try:
        return datetime.date(year, month, day).isoformat()
    except ValueError:
        return text


def _time(text: str) -> str:
    value = re.sub(r"\s+", "", text)
    match = (
        _TIME_COMPACT.match(value)
        if value.isdigit() and len(value) in (3, 4)
        else _TIME.match(value)
    )
    if match is None:
        return text
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    if hour > 23 or minute > 59:
        return text
    return f"{hour:02d}:{minute:02d}"


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
    if kind == "date":
        return _date(text)
    if kind == "time":
        return _time(text)
    return text  # email
