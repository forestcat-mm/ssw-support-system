"""提出書類一覧表（表紙・第1表・第2表・第3表）の生成（T42 の追加、ADR-0033 の追記）。

入管の提出書類一覧・確認表の Excel（application_sets/sources）に値だけを書き（制約3）、
LibreOffice で PDF にして、提出用の統合 PDF の先頭に綴じる。
- 表紙: 申請人の氏名（F47）・特定技能所属機関の名称（F48）。
- 提出確認欄: 行ごとの「有」「無」の選んだ側に〇を付ける（一覧表の表紙の留意事項３。
  T42-D18 の直し）。
  〇は PDF に変換する直前の一時の写しでだけ図形として足す（form_xlsx.with_circles。ADR-0032 の
  例外の一覧）。値は表紙の氏名・名称だけを書く。印を付けない行には何も足さない。
- 表紙の氏名・名称の欄は、同じ一時の写しで縦の配置を「下」にする（行が高く、元の「上」では字が
  線から浮くため）。
- 頁の割り方: 同じ一時の写しで、横 1 頁に収め（縦は自動）、Excel で印刷したときの行で改頁する
  （一覧の定義の sources の pdf_page_breaks。application_sets/sources/page_breaks.json）。
  游ゴシックを Noto Sans CJK JP で描いても、LibreOffice では頁に入る行の数が Excel と少し違うため。
- 初期値（T42-D19）: 統合 PDF に綴じる書類がある行は「有」、当てはまるが綴じる書類が無い行は「無」、
  当てはまるかを判断しない行（実績のある機関の書類など）は空。人が画面で直せる。
- 作る表（T42-D20）: 第1表（表紙を含む）は必ず。ほかの表は、印（有・無）の付いた行があるものだけ。
  既に特定技能外国人を受け入れている機関の第2表の行は、初期値を空にする（提出不要。表紙の留意事項１）。
"""

from __future__ import annotations

import datetime
import enum
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.conf import settings

from apps.contracts.models import EmploymentContract
from apps.hosts.models import HostOrganization
from services import form_convert, form_xlsx
from services.application_sets import Overview, Status

TABLE2 = "_t2_"  # 第2表のファイルの名前の印


class Mark(enum.StrEnum):
    YES = "yes"
    NO = "no"
    NONE = ""


def sources_root() -> Path:
    return Path(settings.REPO_ROOT) / "application_sets" / "sources"


def fiscal_year_start(on: datetime.date) -> datetime.date:
    return datetime.date(on.year if on.month >= 4 else on.year - 1, 4, 1)


def already_accepting(host: HostOrganization, on: datetime.date) -> bool:
    """受入れ機関の印（同一年度内に特定技能外国人を既に受け入れている。T42-D25 の直し）。

    印が有り、受け入れ始めた日が無いか基準日以前なら、第2表の書類は提出不要（表紙の留意事項１）。
    """
    since = host.accepts_ssw_workers_since
    return host.accepts_ssw_workers and (since is None or since <= on)


def suggested_since(
    host: HostOrganization, on: datetime.date, *, worker_id: int | None = None
) -> datetime.date | None:
    """台帳で、同じ年度にほかの外国人とその受入れ機関の雇用契約がある（印の初期値の提案だけ）。

    台帳の外の外国人もいるので、判定には使わない。最も早い契約の開始日を返す。
    """
    start = fiscal_year_start(on)
    contracts = EmploymentContract.objects.filter(
        host_organization=host, current_starts_on__lte=on
    ).exclude(terminated_on__lt=start)
    if worker_id is not None:
        contracts = contracts.exclude(worker_id=worker_id)
    first = contracts.order_by("current_starts_on").values_list("current_starts_on", flat=True)
    return first.first()


def is_table2(row: Mapping[str, Any]) -> bool:
    return TABLE2 in str(row.get("check", {}).get("file", ""))


def default_marks(overview: Overview, bound_rows: set[str], *, skip_table2: bool) -> dict[str, str]:
    """行ごとの有・無の初期値（行の ID → Mark）。"""
    marks: dict[str, str] = {}
    for state in overview.rows:
        row = state.row
        if skip_table2 and is_table2(row):
            marks[row["id"]] = Mark.NONE
        elif row["id"] in bound_rows:
            marks[row["id"]] = Mark.YES
        elif state.applicable is True and state.status is not Status.ALTERNATIVE:
            marks[row["id"]] = Mark.NO
        else:
            marks[row["id"]] = Mark.NONE
    return marks


@dataclass(frozen=True)
class Workbook:
    file: str
    values: dict[str, object]  # 表紙の氏名・名称（値だけ）
    circles: tuple[str, ...] = ()  # 〇を付けるセル（「シート!セル」）
    page_breaks: tuple[int, ...] = ()  # PDF の各頁の最初の行（Excel で印刷したときの改頁）


def workbooks(
    overview: Overview, marks: Mapping[str, str], *, worker_name: str, host_name: str
) -> list[Workbook]:
    """作る表と、書く値（第1表は必ず。ほかは印のある行がある表だけ。一覧の定義の順）。"""
    definition = overview.definition
    cover = definition.cover
    by_file: dict[str, dict[str, object]] = {}
    circles: dict[str, list[str]] = {}
    order = list(definition.sources)
    by_file[cover["file"]] = {
        f"{cover['sheet']}!{cover['worker']}": worker_name,
        f"{cover['sheet']}!{cover['host']}": host_name,
    }
    for row in definition.rows:
        mark = marks.get(row["id"], Mark.NONE)
        check = row.get("check")
        if not check or mark == Mark.NONE:
            continue
        by_file.setdefault(check["file"], {})
        chosen = check["yes"] if mark == Mark.YES else check["no"]
        circles.setdefault(check["file"], []).append(f"{check['sheet']}!{chosen}")
    return [
        Workbook(
            file,
            by_file[file],
            tuple(circles.get(file, ())),
            definition.page_breaks.get(file, ()),
        )
        for file in order
        if file in by_file
    ]


def render(books: list[Workbook], workdir: Path) -> list[Path]:
    """表ごとの PDF（Excel に値だけを書いて変換する）。"""
    pdfs: list[Path] = []
    for index, book in enumerate(books):
        folder = workdir / f"list-{index}"
        folder.mkdir(parents=True, exist_ok=True)
        filled = folder / book.file
        form_xlsx.write_cells(sources_root() / book.file, filled, book.values)
        # 〇・横 1 頁に収める設定・改頁は、変換の直前の一時の写しでだけ足す（書いたブックは
        # 値だけ。ADR-0032 の例外の一覧）
        copy = folder / "for-pdf" / book.file
        form_xlsx.with_circles(filled, copy, list(book.circles))
        # 表紙の氏名・名称の欄は行が高く縦の配置が「上」なので、字が線から浮く。一時の写しで「下」に
        form_xlsx.with_print_layout(
            copy,
            copy,
            list(book.page_breaks),
            bottom=[form_xlsx.split_target(target)[1] for target in book.values],
        )
        pdfs.append(form_convert.convert(copy))
    return pdfs
