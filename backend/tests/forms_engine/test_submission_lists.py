"""提出書類一覧表の生成（T42 の追加）。

- 提出確認欄の初期値は状態から（綴じる書類がある行は有、当てはまるが無い行は無）。人が直せる。
- 入管の Excel に値だけを書く（選ばない側の「有」「無」を消す。表紙に氏名と名称）。
- 統合 PDF の先頭に綴じ、印を生成履歴に残す。
"""

import datetime
import io
import zipfile
from pathlib import Path

import pytest
from pypdf import PdfReader

from apps.forms_engine.models import SubmissionBundle
from services import application_sets, form_xlsx, submission_bundles, submission_lists
from tests.core.factories import make_user, verified_client
from tests.forms_engine.test_form_application import make_case

pytestmark = pytest.mark.django_db

BASIS = datetime.date(2026, 10, 18)


def test_definitions_have_the_check_cells() -> None:
    definition = application_sets.definition("coe", BASIS)
    assert definition.cover == {
        "file": "coe_t1_001472567.xlsx",
        "sheet": "表紙",
        "worker": "F47",
        "host": "F48",
    }
    first = definition.rows[0]
    assert first["check"] == {
        "file": "coe_t1_001472567.xlsx",
        "sheet": "表紙",
        "yes": "G53",
        "no": "H53",
    }
    assert all("check" in row for row in definition.rows)


def test_marks_and_values_of_the_workbooks() -> None:
    contract = make_case()
    overview = application_sets.overview(contract.worker, "chg", BASIS)
    admin = make_user("admin42c", roles=("admin",))
    outline = submission_bundles.outline(overview, admin)
    marks = outline.default_marks(True)
    assert marks["chg_t1-2-1"] == submission_lists.Mark.YES  # 一覧表そのもの
    assert marks["chg_t1-8-1"] == submission_lists.Mark.NO  # 健康診断個人票（添付が無い）
    assert marks["chg_t2_1-1-1"] == submission_lists.Mark.NONE  # 実績のある機関（判断しない）
    books = submission_lists.workbooks(
        overview, marks, worker_name="WANG LINA", host_name="テスト株式会社"
    )
    files = [book.file for book in books]
    assert files[0] == "chg_t1_001472570.xlsx"
    assert "chg_t2_1_001446575.xlsx" not in files  # 印のある行が無い表は作らない
    values = books[0].values
    assert values["表紙!F47"] == "WANG LINA" and values["表紙!F48"] == "テスト株式会社"
    assert "表紙!H54" not in values and "表紙!G54" not in values  # 提出確認欄に値は書かない
    assert "表紙!G54" in books[0].circles  # 有（一覧表の行）に〇
    assert "表紙!H65" in books[0].circles  # 無（健康診断個人票）に〇


def cell_text(path: Path, sheet: str, ref: str) -> str:
    import openpyxl  # type: ignore[import-untyped]

    workbook = openpyxl.load_workbook(path)
    return str(workbook[sheet][ref].value or "")


def test_render_writes_only_values(tmp_path: Path) -> None:
    contract = make_case()
    overview = application_sets.overview(contract.worker, "chg", BASIS)
    books = submission_lists.workbooks(
        overview,
        {"chg_t1-2-1": "yes", "chg_t1-8-1": "no"},
        worker_name="WANG LINA",
        host_name="テスト株式会社",
    )
    pdfs = submission_lists.render(books, tmp_path)
    assert len(pdfs) == len(books) and all(p.read_bytes().startswith(b"%PDF") for p in pdfs)
    filled = tmp_path / "list-0" / books[0].file
    assert cell_text(filled, "表紙", "F47") == "WANG LINA"
    assert cell_text(filled, "表紙", "G54") == "有" and cell_text(filled, "表紙", "H54") == "無"
    # 〇は変換の一時の写しにだけ図形で足す（書いたブックには無い）
    copy = tmp_path / "list-0" / "for-pdf" / books[0].file
    with zipfile.ZipFile(copy) as archive:
        drawing = archive.read("xl/drawings/drawing1.xml").decode()
    assert drawing.count('prst="ellipse"') == 2
    with zipfile.ZipFile(filled) as archive:
        assert 'prst="ellipse"' not in archive.read("xl/drawings/drawing1.xml").decode()
    source = submission_lists.sources_root() / books[0].file
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(filled) as written:
        changed = [
            name for name in original.namelist() if original.read(name) != written.read(name)
        ]
    assert changed == ["xl/worksheets/sheet1.xml"]  # セルの値だけ（制約3）
    form_xlsx.check_targets(filled, ["表紙!F47"])


def test_bundle_starts_with_the_list_and_keeps_the_marks() -> None:
    contract = make_case()
    admin = make_user("admin42c", roles=("admin",))
    overview = application_sets.overview(contract.worker, "chg", BASIS)
    outline = submission_bundles.outline(overview, admin)
    marks = outline.default_marks(True) | {"chg_t1-8-1": "yes"}  # 人が直した
    bundle = submission_bundles.build(
        outline, by=admin, accept_warnings=True, make_list=True, marks=marks
    )
    assert bundle.items[0]["source"] == "list" and bundle.items[0]["row"] == "chg_t1-2-1"
    assert bundle.list_marks["chg_t1-8-1"] == "yes"
    reader = PdfReader(io.BytesIO(bundle.file.read()))
    assert str(getattr(reader.outline[0], "title", "")) == "提出書類一覧表"


def test_bundle_screen_offers_the_marks_and_the_list() -> None:
    contract = make_case()
    client = verified_client(make_user("admin42c", roles=("admin",)))
    url = (
        f"/workers/{contract.worker_id}/documents/application/submission/"
        "?set_id=chg&basis_on=2026-10-18"
    )
    page = client.get(url)
    content = page.content.decode()
    assert 'name="make_list"' in content and 'name="mark-chg_t1-8-1"' in content
    assert 'id="submission-photo"' in content
    form = page.context["form"]
    data = {
        page.context["once_field"]: page.context["once_token"],
        "set_id": "chg",
        "basis_on": "2026-10-18",
        "accept_warnings": "on",
        "make_list": "on",
        "action": "make",
        **{name: form.initial.get(name, "") for name in form.fields if name.startswith("mark-")},
    }
    assert client.post(url, data).status_code == 302
    assert SubmissionBundle.objects.get().items[0]["source"] == "list"


SELF_SUPPORT_ROWS: tuple[str, ...] = tuple(f"chg_t2_2-2-{n}" for n in range(1, 7))


def test_full_support_excludes_the_self_support_documents_of_table2() -> None:
    """全部委託の申請では、受入れ機関が自ら支援する場合の書類（第1-11-2号・1-11-3号・
    1-20号・1-22号・同程度の説明書と立証資料）が、一覧・一覧表の印・統合 PDF の対象から外れる。"""
    contract = make_case(full_support=True)
    admin = make_user("admin42e", roles=("admin",))
    overview = application_sets.overview(contract.worker, "chg", BASIS)
    shown = {state.row["id"] for state in overview.rows}
    excluded = {state.row["id"] for state in overview.not_applicable}
    assert set(SELF_SUPPORT_ROWS) <= excluded and not set(SELF_SUPPORT_ROWS) & shown
    outline = submission_bundles.outline(overview, admin)
    assert not set(SELF_SUPPORT_ROWS) & {entry.row_id for entry in outline.entries}
    assert not set(SELF_SUPPORT_ROWS) & set(outline.default_marks(True))


def test_self_support_documents_apply_without_full_support() -> None:
    """自ら支援する（全部委託しない）ときは当てはまる。"""
    other = make_case(full_support=False)
    overview = application_sets.overview(other.worker, "chg", BASIS)
    assert set(SELF_SUPPORT_ROWS) <= {state.row["id"] for state in overview.rows}
