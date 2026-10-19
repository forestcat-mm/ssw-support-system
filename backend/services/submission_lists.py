"""提出書類一覧表（表紙・第1表・第2表・第3表）の生成（T42 の追加、ADR-0033 の追記）。

入管の提出書類一覧・確認表の Excel（application_sets/sources）に値だけを書き（制約3）、
LibreOffice で PDF にして、提出用の統合 PDF の先頭に綴じる。
- 表紙: 申請人の氏名（F47）・特定技能所属機関の名称（F48）。
- 提出確認欄: 行ごとの「有」「無」。選んだ側の文字を残し、ほかの側を全角空白で消す（〇で囲む代わり。
  第1-17号の選択肢と同じ。T42-D18）。印を付けない行は書かない。
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

ERASE = "　"  # 選ばない側の印刷済みの「有」「無」を消す（T31-D3 と同じ）
TABLE2 = "_t2_"  # 第2表のファイルの名前の印


class Mark(enum.StrEnum):
    YES = "yes"
    NO = "no"
    NONE = ""


def sources_root() -> Path:
    return Path(settings.REPO_ROOT) / "application_sets" / "sources"


def fiscal_year_start(on: datetime.date) -> datetime.date:
    return datetime.date(on.year if on.month >= 4 else on.year - 1, 4, 1)


def already_accepting(host: HostOrganization, worker_id: int, on: datetime.date) -> bool:
    """同じ年度に、ほかの外国人とその受入れ機関の雇用契約がある（第2表の書類は提出不要。T42-D25）。"""
    start = fiscal_year_start(on)
    return (
        EmploymentContract.objects.filter(host_organization=host, current_starts_on__lte=on)
        .exclude(worker_id=worker_id)
        .exclude(terminated_on__lt=start)
        .exists()
    )


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
    values: dict[str, object]


def workbooks(
    overview: Overview, marks: Mapping[str, str], *, worker_name: str, host_name: str
) -> list[Workbook]:
    """作る表と、書く値（第1表は必ず。ほかは印のある行がある表だけ。一覧の定義の順）。"""
    definition = overview.definition
    cover = definition.cover
    by_file: dict[str, dict[str, object]] = {}
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
        values = by_file.setdefault(check["file"], {})
        erased = check["no"] if mark == Mark.YES else check["yes"]
        values[f"{check['sheet']}!{erased}"] = ERASE
    return [Workbook(file, by_file[file]) for file in order if file in by_file]


def render(books: list[Workbook], workdir: Path) -> list[Path]:
    """表ごとの PDF（Excel に値だけを書いて変換する）。"""
    pdfs: list[Path] = []
    for index, book in enumerate(books):
        folder = workdir / f"list-{index}"
        folder.mkdir(parents=True, exist_ok=True)
        filled = folder / book.file
        form_xlsx.write_cells(sources_root() / book.file, filled, book.values)
        pdfs.append(form_convert.convert(filled))
    return pdfs
