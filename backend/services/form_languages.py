"""帳票の言語（日本語・日中併記。T38、ADR-0027）。

- 中国語の様式は、入管が配布する日中併記の版をそのまま使う（翻訳はしない）。併記の版の日本語の文が
  現行の日本語の版と同じ様式（第1-5号・第5-8号・第5-9号）だけを読み込んでいる。
- 第1-6号・第1-17号の併記の版は、現行の版と文が違う（旧い版）ので使わない。支援言語が中国語の
  外国人の生成の画面に注意を出す（人の確認待ち。ADR-0027 の「未確認の点」）。
- 交付記録の「書面の言語」の初期値は、生成物の版の言語から出す。
- 第1-6号・第1-17号には、自作の中国語の訳文の版（言語 zh-hans。T43、ADR-0035）がある。日本語の版とは
  別の生成物（別の PDF）で、生成の画面の「様式の言語」で選んで作る（初期値は日本語のまま）。
"""

from __future__ import annotations

from django.utils.translation import gettext_lazy as _

from apps.forms_engine.models import FormOwner, FormTemplate
from apps.workers.models import ForeignWorker

JA = "ja"
JA_ZH = "ja-zh"
ZH_TRANSLATION = "zh-hans"  # 自作の中国語の訳文の版（T43）
LABELS = {
    JA: _("Japanese"),
    JA_ZH: _("Japanese and Chinese (bilingual)"),
    ZH_TRANSLATION: _("Chinese (simplified) translation (self-made)"),
}
# 様式の版の言語 → 交付記録の書面の言語（支援言語のコード）
DELIVERY_LANGUAGES: dict[str, tuple[str, ...]] = {
    JA: ("ja",),
    JA_ZH: ("ja", "zh-hans"),
    ZH_TRANSLATION: ("zh-hans",),
}
# 日本語の様式とは別の書類になる訳文の版の言語（申請の書類の一覧では日本語の版で生成の有無を見る）
TRANSLATIONS = frozenset({ZH_TRANSLATION})
ORIGINALS = (JA, JA_ZH)
CHINESE = "zh-hans"
# 併記の版が現行の日本語の版と違うため、日本語の版だけを作る様式（T38-D1）
OUTDATED_BILINGUAL = frozenset({"1-6", "1-17"})


def available(form_id: str) -> list[str]:
    """読み込み済みの共通の版の言語（日本語を先に）。"""
    found = set(
        FormTemplate.objects.filter(form_id=form_id, owner=FormOwner.COMMON).values_list(
            "language", flat=True
        )
    )
    return sorted(found, key=lambda code: (code != JA, code))


def is_chinese(worker: ForeignWorker) -> bool:
    language = worker.support_language
    return language is not None and language.code == CHINESE


def default_for(worker: ForeignWorker, form_id: str) -> str:
    """初期値: 支援言語が中国語で併記の版があれば日中併記、ほかは日本語。"""
    if is_chinese(worker) and JA_ZH in available(form_id):
        return JA_ZH
    return JA


def choices(form_id: str) -> list[tuple[str, str]]:
    return [(code, str(LABELS.get(code, code))) for code in available(form_id)]


def label(code: str) -> str:
    return str(LABELS.get(code, code))


def outdated_notice(worker: ForeignWorker, form_id: str) -> bool:
    """支援言語が中国語で、併記の版を使わない様式（画面に注意を出す）。"""
    return is_chinese(worker) and form_id in OUTDATED_BILINGUAL


def delivery_language_codes(template_language: str) -> tuple[str, ...]:
    return DELIVERY_LANGUAGES.get(template_language, (JA,))
