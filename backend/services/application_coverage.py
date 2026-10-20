"""申請の書類の一覧の全部の行の対応の洗い出し（T42 の追加。.checks/coverage-T42.md を作る）。

行ごとに、システムで「生成できる・記録の添付で綴じる・受入れ機関の書類の保管庫で綴じる・その場で
アップロードして綴じる・紙だけ（綴じない）」を、申請の書類の一覧と統合 PDF と同じ規則で分ける。
参考様式があるのに生成できない行は、様式の番号ごとにまとめる（作るかは人が決める）。
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from typing import Any

from services import application_sets, host_documents, submission_bundles

FORM_NUMBER = re.compile(
    r"(?:分野)?参考様式\s*第?\s*([0-9０-９]+[－\-][0-9０-９]+(?:[－\-][0-9０-９]+)?)号"
)


@dataclass(frozen=True)
class CoverageRow:
    set_id: str
    row: dict[str, Any]
    way: str  # generate / list / record / host_document / upload / paper
    reference_form: str  # 生成できない参考様式の番号（無ければ空）


WAYS = {
    "generate": "生成できる",
    "list": "システムで作る（提出書類一覧表。統合 PDF の先頭）",
    "record": "記録の添付で綴じる",
    "host_document": "受入れ機関の書類の保管庫で綴じる",
    "upload": "その場でアップロードして綴じる",
    "paper": "紙だけ（綴じない）",
}


def classify(row: dict[str, Any]) -> tuple[str, str]:
    how = row["how"]
    form = str(row.get("form", "")).replace("\n", "")
    number = ""
    found = FORM_NUMBER.search(form)
    if found and "generate" not in how:
        number = found.group(1).translate(str.maketrans("０１２３４５６７８９－", "0123456789-"))
    if "generate" in how:
        return "generate", ""
    if submission_bundles.LIST_MARK in str(row["document"]):
        return "list", ""  # T42-D4 を「システムで作る」に変えた後の判定
    if host_documents.kinds_for_row(row):
        return "host_document", ""
    if "attachment" in how:
        return "record", ""  # 健康診断個人票（第1-3号）などは医療機関などが書く
    if submission_bundles.NOT_BOUND_MARK in str(row["document"]):
        return "paper", ""
    return "upload", number


def rows(on: datetime.date) -> list[CoverageRow]:
    found: list[CoverageRow] = []
    for set_id in application_sets.SET_IDS:
        for row in application_sets.definition(set_id, on).rows:
            way, number = classify(row)
            found.append(CoverageRow(set_id, row, way, number))
    return found


def markdown(on: datetime.date) -> str:
    items = rows(on)
    lines = [
        "# 申請の書類の一覧の行の対応（T42 の追加）",
        "",
        f"`python manage.py application_coverage` が作る（基準日 {on}。"
        "申請の書類の一覧と統合 PDF と同じ規則）。",
        "",
        "## 数",
        "",
        "| 申請 | " + " | ".join(WAYS.values()) + " | 計 |",
        "|---|" + "---|" * (len(WAYS) + 1),
    ]
    for set_id in application_sets.SET_IDS:
        mine = [item for item in items if item.set_id == set_id]
        counts = [str(sum(1 for item in mine if item.way == way)) for way in WAYS]
        lines.append(f"| {set_id} | " + " | ".join(counts) + f" | {len(mine)} |")
    lines += ["", "## 生成できない参考様式（作るかは人が決める）", ""]
    by_form: dict[str, list[CoverageRow]] = {}
    for item in items:
        if item.reference_form:
            by_form.setdefault(item.reference_form, []).append(item)
    lines += ["| 様式 | 書類 | 行 |", "|---|---|---|"]
    for number in sorted(by_form, key=lambda n: [int(p) for p in n.split("-")]):
        first = by_form[number][0].row
        ids = "、".join(item.row["id"] for item in by_form[number])
        lines.append(f"| 第{number}号 | {first['document'].splitlines()[0][:50]} | {ids} |")
    lines += ["", "## 全部の行", ""]
    lines += ["| 申請 | 行 | 表 | 番号 | 書類 | 様式・発行機関 | 要否 | 対応 |"]
    lines += ["|---|---|---|---|---|---|---|---|"]
    for item in items:
        row = item.row
        document = str(row["document"]).replace("\n", " ").replace("|", "／")[:60]
        form = str(row.get("form", "")).replace("\n", "").replace("|", "／")
        lines.append(
            f"| {item.set_id} | {row['id']} | {row['table']} | {row['number']} | {document} | "
            f"{form} | {row['need']} | {WAYS[item.way]} |"
        )
    return "\n".join(lines) + "\n"
