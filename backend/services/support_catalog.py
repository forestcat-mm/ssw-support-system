"""1号特定技能外国人支援計画の細目の一覧（T16、ADR-0006）。

出典: 出入国在留管理庁 参考様式第1-17号「1号特定技能外国人支援計画書」（EXCEL）
https://www.moj.go.jp/isa/content/001459168.xlsx （2026-10-17 取得）

様式のⅣ欄（支援内容）の区分ごとに、「実施予定・委託の有無・担当者・実施方法」を 1 組で書く
欄（行）がある。区分によって、細目（a, b, c…）ごとに 1 組の区分（2・3ア・3イ・5・8）と、
細目をまとめて 1 組の区分（1・4・6・7・9）がある（様式の結合セルから判断）。
どの区分にも自由記入の行がある。
様式が改正されたら、この一覧を差し替える（旧版の計画は保存した値のまま残る）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from django.db import models
from django.utils.translation import gettext_lazy as _

FORM_SOURCE = "参考様式第1-17号（services/data/support_plan_1-17.json の url・retrieved_on）"


class SupportItem(models.TextChoices):
    """義務的支援10項目（仕様書 第3章）。番号順。"""

    PRE_GUIDANCE = "pre_guidance", _("pre-entry guidance")
    AIRPORT_TRANSFER = "airport_transfer", _("transfer on entry and departure")
    HOUSING_AND_CONTRACTS = "housing_and_contracts", _("housing and contracts for daily life")
    ORIENTATION = "orientation", _("life orientation")
    PUBLIC_PROCEDURES = "public_procedures", _("accompanying to public procedures")
    JAPANESE_LEARNING = "japanese_learning", _("opportunities to learn Japanese")
    CONSULTATION = "consultation", _("consultations and complaints")
    EXCHANGE = "exchange", _("exchange with Japanese people")
    JOB_CHANGE = "job_change", _("support for changing jobs")
    PERIODIC_INTERVIEW = "periodic_interview", _("periodic interviews and reports to authorities")


# 様式のⅣ欄の区分の番号（公的手続への同行は、生活オリエンテーション〔4〕の b に含まれる）
FORM_SECTION = {
    SupportItem.PRE_GUIDANCE: "1",
    SupportItem.AIRPORT_TRANSFER: "2",
    SupportItem.HOUSING_AND_CONTRACTS: "3",
    SupportItem.ORIENTATION: "4",
    SupportItem.PUBLIC_PROCEDURES: "4b",
    SupportItem.JAPANESE_LEARNING: "5",
    SupportItem.CONSULTATION: "6",
    SupportItem.EXCHANGE: "7",
    SupportItem.JOB_CHANGE: "8",
    SupportItem.PERIODIC_INTERVIEW: "9",
}

# 実施予定時間（合計の時間）の欄がある項目
ITEMS_WITH_HOURS = frozenset({SupportItem.PRE_GUIDANCE, SupportItem.ORIENTATION})

# 実施言語・通訳者の欄がある項目（様式のⅣ欄 1・4・6・9）
ITEMS_WITH_LANGUAGE = frozenset(
    {
        SupportItem.PRE_GUIDANCE,
        SupportItem.ORIENTATION,
        SupportItem.CONSULTATION,
        SupportItem.PERIODIC_INTERVIEW,
    }
)


class Method(models.TextChoices):
    """実施方法のチェックボックス（様式の文言）。"""

    IN_PERSON = "in_person", _("in person")
    VIDEO_CALL = "video_call", _("video call")
    VIDEO_OR_DVD = "video_or_dvd", _("video call or watching videos such as DVDs")
    ONLINE = "online", _("online")
    ARRIVAL_AIRPORT = "arrival_airport", _("airport of arrival")
    DEPARTURE_AIRPORT = "departure_airport", _("airport of departure")
    TRANSPORT = "transport", _("means of transfer")
    INFORMATION = "information", _("information on the procedure")
    ACCOMPANY = "accompany", _("accompanying to the procedure as needed")
    OTHER = "other", _("other")


# 記述を添える実施方法（「その他（　）」「（　空港）」「送迎方法（　）」）
METHODS_WITH_NOTE = frozenset(
    {Method.OTHER, Method.ARRIVAL_AIRPORT, Method.DEPARTURE_AIRPORT, Method.TRANSPORT}
)


@dataclass(frozen=True)
class Row:
    """様式の「実施予定・委託・担当者・実施方法」1 組の行。"""

    code: str  # 例: "1.main" / "2.a" / "3a.b" / "8.free"
    item: SupportItem
    letters: tuple[str, ...]  # この行が受け持つ細目（自由記入は空）
    methods: tuple[Method, ...]  # チェックボックスの選択肢。空なら実施方法は文章で書く

    @property
    def is_free(self) -> bool:
        return self.code.endswith(".free")


# 細目の文言（様式のまま。データとして持ち、様式の改正のときに差し替える）
_DATA_FILE = Path(__file__).resolve().parent / "data" / "support_plan_1-17.json"
_DATA = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
ACTIVITY_LABELS: dict[str, str] = _DATA["labels"]
SECTION_TITLES: dict[str, str] = _DATA["sections"]

_GUIDANCE = (Method.IN_PERSON, Method.VIDEO_CALL, Method.OTHER)
_PROCEDURE = (Method.INFORMATION, Method.ACCOMPANY, Method.OTHER)
_I = SupportItem

ROWS: tuple[Row, ...] = (
    Row("1.main", _I.PRE_GUIDANCE, tuple("abcdefghij"), _GUIDANCE),
    Row("1.free", _I.PRE_GUIDANCE, (), ()),
    Row("2.a", _I.AIRPORT_TRANSFER, ("a",), (Method.ARRIVAL_AIRPORT, Method.TRANSPORT)),
    Row("2.b", _I.AIRPORT_TRANSFER, ("b",), (Method.DEPARTURE_AIRPORT, Method.TRANSPORT)),
    Row("2.free", _I.AIRPORT_TRANSFER, (), ()),
    Row("3a.a", _I.HOUSING_AND_CONTRACTS, ("a",), ()),
    Row("3a.b", _I.HOUSING_AND_CONTRACTS, ("b",), ()),
    Row("3a.c", _I.HOUSING_AND_CONTRACTS, ("c",), ()),
    Row("3a.free", _I.HOUSING_AND_CONTRACTS, (), ()),
    Row("3b.a", _I.HOUSING_AND_CONTRACTS, ("a",), _PROCEDURE),
    Row("3b.b", _I.HOUSING_AND_CONTRACTS, ("b",), _PROCEDURE),
    Row("3b.c", _I.HOUSING_AND_CONTRACTS, ("c",), _PROCEDURE),
    Row("3b.free", _I.HOUSING_AND_CONTRACTS, (), ()),
    Row(
        "4.main",
        _I.ORIENTATION,
        tuple("abcdef"),
        (Method.IN_PERSON, Method.VIDEO_OR_DVD, Method.OTHER),
    ),
    Row("4.free", _I.ORIENTATION, (), ()),
    Row("5.a", _I.JAPANESE_LEARNING, ("a",), ()),
    Row("5.b", _I.JAPANESE_LEARNING, ("b",), ()),
    Row("5.c", _I.JAPANESE_LEARNING, ("c",), ()),
    Row("5.free", _I.JAPANESE_LEARNING, (), ()),
    Row("6.main", _I.CONSULTATION, ("a", "b"), ()),
    Row("6.free", _I.CONSULTATION, (), ()),
    Row("7.main", _I.EXCHANGE, ("a", "b"), ()),
    Row("7.free", _I.EXCHANGE, (), ()),
    *(Row(f"8.{letter}", _I.JOB_CHANGE, (letter,), ()) for letter in "abcdefg"),
    Row("8.free", _I.JOB_CHANGE, (), ()),
    Row("9.main", _I.PERIODIC_INTERVIEW, tuple("abcd"), (Method.IN_PERSON, Method.ONLINE)),
    Row("9.free", _I.PERIODIC_INTERVIEW, (), ()),
)

ROWS_BY_CODE: dict[str, Row] = {row.code: row for row in ROWS}


def rows_of(item: SupportItem | str) -> list[Row]:
    return [row for row in ROWS if row.item == item]


def section_prefix(code: str) -> str:
    """行の符号から細目の文言の接頭辞（"3a.b" → "3a"）。"""
    return code.split(".")[0]


def labels_of(row: Row) -> list[tuple[str, str]]:
    """行が受け持つ細目の（符号, 文言）。"""
    prefix = section_prefix(row.code)
    return [(letter, str(ACTIVITY_LABELS[f"{prefix}.{letter}"])) for letter in row.letters]


@dataclass(frozen=True)
class Section:
    """様式のⅣ欄の区分（1〜9。3 はアとイ）。表示と入力はこの順序で並べる。"""

    code: str  # "1" / "3a" / "3b" / …
    item: SupportItem
    title: str
    rows: tuple[Row, ...]


SECTIONS: tuple[Section, ...] = tuple(
    Section(
        code=code,
        item=next(row.item for row in ROWS if section_prefix(row.code) == code),
        title=title,
        rows=tuple(row for row in ROWS if section_prefix(row.code) == code),
    )
    for code, title in SECTION_TITLES.items()
)
