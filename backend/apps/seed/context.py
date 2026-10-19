"""seed_dummy の共通部分（基準日・乱数・台帳・定数）。builders と support_builders から使う。"""

from __future__ import annotations

import io
import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from django.contrib.contenttypes.models import ContentType
from django.db import models
from pypdf import PdfWriter

from apps.contracts.models import EmploymentContract
from apps.core.models import Organization, Staff
from apps.hosts.models import HostOrganization, HostSite
from apps.seed.models import SeedObject
from apps.workers.models import ForeignWorker

SEED = 20261016
DUMMY_MARK = "（ダミー）"
DUMMY_USERNAME = "dummy_tanto"
DUMMY_USERNAME_2 = "dummy_tanto2"  # 2 人目の担当者（鈴木 次郎）


def _dummy_pdf() -> bytes:
    """ダミーの添付の PDF（空白の A4 の 1 頁。提出用の統合 PDF に綴じられる。T42-D13）。"""
    writer = PdfWriter()
    writer.add_blank_page(width=595.276, height=841.89)
    writer.add_metadata({"/Title": "dummy document for development"})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


DUMMY_PDF = _dummy_pdf()


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
