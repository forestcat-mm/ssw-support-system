"""UI 文言リソースの更新漏れ検出（.po と .mo の不一致、未翻訳ラベル）。"""

import gettext
import subprocess
from pathlib import Path

import pytest
from django.apps import apps
from django.conf import settings
from django.db.models import Model
from django.utils import translation
from django.utils.functional import Promise

PO_FILES = sorted(Path(settings.LOCALE_PATHS[0]).glob("*/LC_MESSAGES/*.po"))


def _catalog(mo: Path) -> dict[object, str]:
    with mo.open("rb") as f:
        return dict(gettext.GNUTranslations(f)._catalog)  # type: ignore[attr-defined]


@pytest.mark.parametrize("po", PO_FILES, ids=str)
def test_compiled_mo_matches_po(po: Path, tmp_path: Path) -> None:
    fresh = tmp_path / "fresh.mo"
    subprocess.run(["msgfmt", "--check", "-o", str(fresh), str(po)], check=True)
    assert _catalog(fresh) == _catalog(po.with_suffix(".mo")), (
        f"{po.with_suffix('.mo')} が古い。make messages を実行してコミットする"
    )


def _labels(model: type[Model]) -> list[Promise]:
    meta = model._meta
    labels: list[object] = [meta.verbose_name, meta.verbose_name_plural]
    for field in meta.get_fields():
        labels.append(getattr(field, "verbose_name", None))
        labels.extend(label for _, label in getattr(field, "choices", None) or [])
    return [label for label in labels if isinstance(label, Promise)]


APP_MODELS = [m for m in apps.get_models() if m.__module__.startswith("apps.") and m._meta.managed]


@pytest.mark.parametrize("model", APP_MODELS, ids=str)
def test_app_labels_are_translated(model: type[Model]) -> None:
    with translation.override("ja"):
        untranslated = [str(label) for label in _labels(model) if str(label).isascii()]
    assert untranslated == [], "locale/ja の .po に訳を追加して make messages を実行する"


@pytest.mark.parametrize("po", PO_FILES, ids=str)
def test_po_has_no_fuzzy_entries(po: Path) -> None:
    """fuzzy は .mo に入らず、印だけ消すと誤訳が出るため残さない。"""
    assert "#, fuzzy" not in po.read_text(encoding="utf-8")
