"""UI 文言リソースの更新漏れ検出（.po と .mo の不一致、未翻訳、make messages の実行忘れ）。"""

import gettext
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from django.apps import apps
from django.conf import settings
from django.core.management import call_command
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


def parse_po(path: Path) -> dict[str, str]:
    """.po を {msgid: msgstr} に読む（複数行・複数形は先頭の形だけ。ヘッダーは除く）。"""
    entries: dict[str, str] = {}
    msgid: list[str] | None = None
    msgstr: list[str] | None = None
    current: list[str] | None = None

    def flush() -> None:
        if msgid is not None and "".join(msgid):
            entries["".join(msgid)] = "".join(msgstr or [])

    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("msgid "):
            flush()
            msgid, msgstr = [_unquote(line[6:])], None
            current = msgid
        elif line.startswith("msgid_plural "):
            current = None
        elif line.startswith("msgstr ") or line.startswith("msgstr[0] "):
            msgstr = [_unquote(line.split(" ", 1)[1])]
            current = msgstr
        elif line.startswith("msgstr["):
            current = None
        elif line.startswith('"') and current is not None:
            current.append(_unquote(line))
    flush()
    return entries


def _unquote(text: str) -> str:
    return re.sub(r'^"|"$', "", text.strip())


@pytest.mark.parametrize("po", PO_FILES, ids=str)
def test_po_has_no_untranslated_entries(po: Path) -> None:
    untranslated = [msgid for msgid, msgstr in parse_po(po).items() if not msgstr]
    assert untranslated == [], "訳が空の行がある。.po に訳を書いて make messages を実行する"


def test_po_is_up_to_date_with_sources(
    tmp_path: Path, settings: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """コードとテンプレートから抽出し直した msgid が、コミット済みの .po と一致する。

    makemessages は今のフォルダの locale にも書くので、ソースの写しの中で流す（本物の .po を
    書き直すと、並列で .po を読むほかのテストと重なって落ちるため。2026-10-20）。
    """
    locale = Path(settings.LOCALE_PATHS[0])  # type: ignore[attr-defined]
    source = locale.parent
    copy = tmp_path / "backend"
    shutil.copytree(
        source,
        copy,
        ignore=shutil.ignore_patterns("tests", "e2e", "__pycache__", "*.mo", ".pytest_cache"),
    )
    work = copy / locale.relative_to(source)
    settings.LOCALE_PATHS = [work]  # type: ignore[attr-defined]
    monkeypatch.chdir(copy)
    call_command("makemessages", locale=["ja"], no_location=True, no_obsolete=True, verbosity=0)
    for po in PO_FILES:
        fresh = work / po.relative_to(locale)
        missing = set(parse_po(fresh)) - set(parse_po(po))
        stale = set(parse_po(po)) - set(parse_po(fresh))
        assert (missing, stale) == (set(), set()), "make messages を実行して .po をコミットする"


# --- 開発中の文言・ボタンの言葉・同じ文言の別の意味（修正 1 回目 A1・A2・D2）-------------------

TASK_NUMBER = re.compile(r"(?<![A-Za-z0-9])T\d+[a-z]?(?![0-9])")
TEMPLATE_COMMENT = re.compile(r"\{#.*?#\}|\{% comment %\}.*?\{% endcomment %\}", re.S)
# 画面ごとに意味の違う英語の文言。使うときは必ず文脈（pgettext）を付ける
AMBIGUOUS_MSGIDS = {"level", "required", "attached", "not attached", "basis date"}


def po_entries(path: Path) -> list[tuple[str | None, str, str]]:
    """.po を [(msgctxt, msgid, msgstr)] に読む（複数形は先頭の形だけ）。"""
    entries: list[tuple[str | None, str, str]] = []
    context: list[str] | None = None
    msgid: list[str] | None = None
    msgstr: list[str] | None = None
    current: list[str] | None = None

    def flush() -> None:
        if msgid is not None and "".join(msgid):
            entries.append(
                ("".join(context) if context else None, "".join(msgid), "".join(msgstr or []))
            )

    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("msgctxt "):
            flush()
            context, msgid, msgstr = [_unquote(line[8:])], None, None
            current = context
        elif line.startswith("msgid "):
            if msgid is not None:
                flush()
                context = None
            msgid, msgstr = [_unquote(line[6:])], None
            current = msgid
        elif line.startswith("msgid_plural ") or (line.startswith("msgstr[") and msgstr):
            current = None
        elif line.startswith("msgstr ") or line.startswith("msgstr[0] "):
            msgstr = [_unquote(line.split(" ", 1)[1])]
            current = msgstr
        elif line.startswith('"') and current is not None:
            current.append(_unquote(line))
        elif not line.strip():
            flush()
            context, msgid, msgstr, current = None, None, None, None
    flush()
    return entries


@pytest.mark.parametrize("po", PO_FILES, ids=str)
def test_ui_text_has_no_task_numbers(po: Path) -> None:
    """画面の文言に開発中のタスクの番号（T37 など）を出さない。"""
    found = [
        text
        for _c, msgid, msgstr in po_entries(po)
        for text in (msgid, msgstr)
        if TASK_NUMBER.search(text)
    ]
    assert found == []


def test_templates_have_no_task_numbers() -> None:
    templates = sorted(Path(settings.BASE_DIR, "apps").glob("*/templates/**/*.html"))
    assert templates
    found = []
    for path in templates:
        text = TEMPLATE_COMMENT.sub("", path.read_text(encoding="utf-8"))
        found += [f"{path.name}: {m.group(0)}" for m in TASK_NUMBER.finditer(text)]
    assert found == []


@pytest.mark.parametrize("po", PO_FILES, ids=str)
def test_add_buttons_say_tsuika(po: Path) -> None:
    """行を足すボタンは「〜を追加」にそろえる（「〜を足す」にしない）。"""
    assert [msgstr for _c, _i, msgstr in po_entries(po) if msgstr.endswith("を足す")] == []


@pytest.mark.parametrize("po", PO_FILES, ids=str)
def test_ambiguous_msgids_always_have_a_context(po: Path) -> None:
    """意味の違う使い方がある文言は、どこでも文脈を付ける（訳の取り違えを防ぐ）。"""
    entries = po_entries(po)
    assert [i for c, i, _s in entries if i in AMBIGUOUS_MSGIDS and c is None] == []
    contexts = {(c, i) for c, i, _s in entries if i in AMBIGUOUS_MSGIDS}
    assert ("career plan", "level") in contexts and ("generated document", "basis date") in contexts
