"""提出書類一覧表の生成（T42 の追加）。

- 提出確認欄の初期値は状態から（綴じる書類がある行は有、当てはまるが無い行は無）。人が直せる。
- 入管の Excel に値だけを書く（表紙に氏名と名称）。提出確認欄の〇・横 1 頁に収める設定・改頁は
  PDF にする一時の写しにだけ足す（ADR-0032 の例外の一覧）。
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


def test_render_fits_each_table_to_the_page_width(tmp_path: Path) -> None:
    """右の列（提出確認欄）が別の頁にはみ出さず、Excel で印刷したときの行で頁が割れる。"""
    contract = make_case()
    overview = application_sets.overview(contract.worker, "chg", BASIS)
    marks = {row["id"]: "yes" for row in overview.definition.rows}  # すべての表を作る
    books = submission_lists.workbooks(
        overview, marks, worker_name="WANG LINA", host_name="テスト株式会社"
    )
    assert books[0].page_breaks == (49, 67, 72)  # Excel の改頁（page_breaks.json）
    pdfs = submission_lists.render(books, tmp_path)
    for index, (book, pdf) in enumerate(zip(books, pdfs, strict=True)):
        pages = PdfReader(str(pdf)).pages
        assert len(pages) == len(book.page_breaks) + 1, book.file
        for page in pages:
            text = page.extract_text()
            # 頁ごとに、書類の名前の列と提出確認欄の列が同じ頁にある（表紙の 1 頁目を除く）
            if "提出確認欄" in text or "必要書類" in text:
                assert "提出確認欄" in text and "必要書類" in text, book.file
        copy = tmp_path / f"list-{index}" / "for-pdf" / book.file
        with zipfile.ZipFile(copy) as archive:
            sheet = archive.read("xl/worksheets/sheet1.xml").decode()
        assert '<pageSetUpPr fitToPage="1"/>' in sheet
        assert 'fitToWidth="1" fitToHeight="0"' in sheet
        assert sheet.count('man="1"') == len(book.page_breaks)
        # 保存するブック（値を書いたもの）は元の印刷の設定のまま
        with zipfile.ZipFile(tmp_path / f"list-{index}" / book.file) as archive:
            written = archive.read("xl/worksheets/sheet1.xml").decode()
        assert 'fitToWidth="1" fitToHeight="0"' not in written


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


def test_yu_fonts_are_drawn_with_noto_cjk() -> None:
    """一覧表の既定の字体（游ゴシック）・日中併記の様式の游明朝は Noto CJK で描く。"""
    import shutil
    import subprocess

    if shutil.which("fc-match") is None:
        pytest.skip("fontconfig is not installed")
    expected = {
        "游ゴシック": "Noto Sans CJK JP",
        "游ゴシック Light": "Noto Sans CJK JP",
        "Yu Gothic": "Noto Sans CJK JP",
        "游明朝": "Noto Serif CJK JP",
        "Yu Mincho": "Noto Serif CJK JP",
    }
    for family, font in expected.items():
        found = subprocess.run(
            ["fc-match", "-f", "%{family[0]}", family], capture_output=True, text=True, check=True
        )
        assert found.stdout == font, family


def _header_columns(path: Path) -> dict[str, tuple[int, int]]:
    """一覧表のブックの「提出確認欄」「官用欄」の見出しの列の範囲（結合の範囲。1 から数える）。"""
    import re

    with zipfile.ZipFile(path) as archive:
        shared = archive.read("xl/sharedStrings.xml").decode()
        sheet = archive.read("xl/worksheets/sheet1.xml").decode()
    strings = [
        "".join(re.findall(r"<t[^>]*>([^<]*)</t>", item))
        for item in re.findall(r"<si>.*?</si>", shared, re.S)
    ]
    found: dict[str, tuple[int, int]] = {}
    for match in re.finditer(r'<c r="([A-Z]+\d+)"[^>]*?t="s"[^>]*?><v>(\d+)</v>', sheet):
        text = strings[int(match.group(2))]
        for head in ("提出確認欄", "官用欄"):
            if text.startswith(head):  # 後ろはふりがな
                letters, row = form_xlsx.split_ref(match.group(1))
                column = form_xlsx.column_index(letters)
                span = (column, column)
                for left, top, right, _bottom in form_xlsx.merged_ranges(sheet):
                    if left == column and top == row:
                        span = (left, right)
                found[head] = span
    return found


@pytest.mark.parametrize("set_id", ["coe", "chg", "ext"])
def test_circles_are_only_in_the_submission_check_column(set_id: str) -> None:
    """〇を付けるセルは「提出確認欄」の列の中だけで、「官用欄」の列には無い（入管が書く欄）。"""
    definition = application_sets.definition(set_id, BASIS)
    columns: dict[str, dict[str, tuple[int, int]]] = {
        file: _header_columns(submission_lists.sources_root() / file) for file in definition.sources
    }
    checked = 0
    for row in definition.rows:
        check = row["check"]
        heads = columns[check["file"]]
        own, official = heads["提出確認欄"], heads["官用欄"]
        for ref in (check["yes"], check["no"]):
            letters, _row = form_xlsx.split_ref(ref)
            column = form_xlsx.column_index(letters)
            assert own[0] <= column <= own[1], (row["id"], ref)
            assert not official[0] <= column <= official[1], (row["id"], ref)
            checked += 1
    assert checked == 2 * len(definition.rows)


def test_rendered_list_writes_nothing_in_the_official_column(tmp_path: Path) -> None:
    """生成した一覧表で、官用欄の列のセルは元のブックのまま、〇の図形も官用欄に無い。"""
    import re

    contract = make_case()
    overview = application_sets.overview(contract.worker, "chg", BASIS)
    rows = overview.definition.rows
    marks = {row["id"]: ("yes" if index % 2 else "no") for index, row in enumerate(rows)}
    books = submission_lists.workbooks(
        overview, marks, worker_name="WANG LINA", host_name="テスト株式会社"
    )
    submission_lists.render(books, tmp_path)

    def official_cells(xml: str, span: tuple[int, int]) -> list[str]:
        cells = re.findall(r'<c r="([A-Z]+)\d+"[^>]*?(?:/>|>.*?</c>)', xml)
        whole = re.findall(r'<c r="[A-Z]+\d+"[^>]*?(?:/>|>.*?</c>)', xml)
        return [
            cell
            for letters, cell in zip(cells, whole, strict=True)
            if span[0] <= form_xlsx.column_index(letters) <= span[1]
        ]

    for index, book in enumerate(books):
        source = submission_lists.sources_root() / book.file
        official = _header_columns(source)["官用欄"]
        with zipfile.ZipFile(source) as archive:
            before = official_cells(archive.read("xl/worksheets/sheet1.xml").decode(), official)
        with zipfile.ZipFile(tmp_path / f"list-{index}" / book.file) as archive:
            after = official_cells(archive.read("xl/worksheets/sheet1.xml").decode(), official)
        assert after == before, book.file  # 官用欄のセルに書き込みが無い
        with zipfile.ZipFile(tmp_path / f"list-{index}" / "for-pdf" / book.file) as archive:
            drawings = [n for n in archive.namelist() if n.startswith("xl/drawings/drawing")]
            anchors = [
                int(column) + 1  # xdr:col は 0 から数える
                for name in drawings
                for column in re.findall(
                    r"<xdr:oneCellAnchor><xdr:from><xdr:col>(\d+)</xdr:col>"
                    r'(?:(?!</xdr:oneCellAnchor>).)*?prst="ellipse"',
                    archive.read(name).decode(),
                    re.S,
                )
            ]
        assert anchors, book.file
        assert all(not official[0] <= column <= official[1] for column in anchors), book.file
