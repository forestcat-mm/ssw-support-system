"""seed_dummy の共通部分（基準日・乱数・台帳・定数）。builders と support_builders から使う。"""

from __future__ import annotations

import datetime
import io
import random
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from zoneinfo import ZoneInfo

from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from apps.contracts.models import EmploymentContract
from apps.core.models import Organization, Staff
from apps.hosts.models import HostOrganization, HostSite
from apps.seed.models import SeedObject
from apps.workers.models import ForeignWorker

SEED = 20261016
TOKYO = ZoneInfo("Asia/Tokyo")
DUMMY_MARK = "（ダミー）"
DUMMY_USERNAME = "dummy_tanto"
DUMMY_USERNAME_2 = "dummy_tanto2"  # 2 人目の担当者（鈴木 次郎）


@contextmanager
def as_of(day: date, at: time = time(10)) -> Iterator[None]:
    """この中の「今」をその日の時刻にする（生成日・署名日の検査のため。抜けると元に戻す）。

    seed_scenario と seed_documents が使う。監査ログの日時（モデルの既定）は実際の時刻のまま。
    """
    real = timezone.now
    offset = datetime.datetime.combine(day, at, tzinfo=TOKYO) - real()
    timezone.now = lambda: real() + offset
    try:
        yield
    finally:
        timezone.now = real


def _dummy_pdf() -> bytes:
    """ダミーの添付の PDF（空白の A4 の 1 頁。提出用の統合 PDF に綴じられる。T42-D13）。"""
    writer = PdfWriter()
    writer.add_blank_page(width=595.276, height=841.89)
    writer.add_metadata({"/Title": "dummy document for development"})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


DUMMY_PDF = _dummy_pdf()


def dummy_pdf(label: str) -> bytes:
    """書類の名前を書いたダミーの PDF（A4 の 1 頁）。統合 PDF で、どの書類か見分けられるように。

    日本語の字体を埋め込まずに描けるよう、名前は英字で書く（Helvetica）。
    """
    writer = PdfWriter()
    page = writer.add_blank_page(width=595.276, height=841.89)
    lines = ["DUMMY DOCUMENT (development data)", label]

    def escaped(text: str) -> str:
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    body = "".join(
        f"BT /F1 {size} Tf 56 {y} Td ({escaped(text)}) Tj ET\n"
        for (size, y), text in zip(((14, 760), (18, 730)), lines, strict=True)
    )
    stream = DecodedStreamObject()
    stream.set_data(body.encode("latin-1", "replace"))
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_metadata({"/Title": f"dummy document: {label}"})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


@dataclass
class SeedContext:
    base: date  # 基準日
    rng: random.Random = field(default_factory=lambda: random.Random(SEED))
    created: list[models.Model] = field(default_factory=list)
    organization: Organization | None = None
    staff: list[Staff] = field(default_factory=list)
    hosts: list[HostOrganization] = field(default_factory=list)
    sites: list[HostSite] = field(default_factory=list)
    workers: list[ForeignWorker] = field(default_factory=list)
    # 3 社目（飲食料品製造業分野。転籍の場面だけに使う。外国人の割り振りの事業所には含めない）
    extra_sites: list[HostSite] = field(default_factory=list)
    transferred_contract: EmploymentContract | None = None
    # 加入証明なしの場面（T39。4 社目と 31 番。workers・hosts には入れない）
    uncertified_host: HostOrganization | None = None
    uncertified_worker: ForeignWorker | None = None

    def worker(self, number: int) -> ForeignWorker:
        """外国人の番号（1 から。docs/dummy-data.md の表の番号）。"""
        return self.workers[number - 1]

    def keep(self, obj: models.Model) -> models.Model:
        """作った行を台帳に載せる（--reset で消す対象）。"""
        SeedObject.objects.create(
            content_type=ContentType.objects.get_for_model(obj), object_id=obj.pk
        )
        self.created.append(obj)
        return obj

    def days(self, n: int) -> date:
        return self.base + timedelta(days=n)
