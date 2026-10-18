"""ダミーの生成物の場面（T39-D2。管理コマンド seed_documents）。

seed_dummy の後に実行する。生成は PDF の変換（LibreOffice）を伴うので seed_dummy には入れず、
別のコマンドにした。何度実行しても同じ（作成済みなら何もしない）。生成物は seed-reset で消える
（ダミーの外国人の生成物。fix 0）。

| 外国人 | 様式 | 場面 |
|---|---|---|
| 2 番（中国語） | 第1-5号（日中併記） | 署名待ち・中国語の外国人 |
| 26 番（日本語） | 第1-5号 | 署名済み（スキャンを添付） |
| 3 番（中国語） | 第1-6号（日本語だけ。T38-D1） | 署名待ち・署名済み書類の回収のタスク |
| 15 番（食品） | 第13-1号 | 警告つき（キャリアアッププラン未交付） |

31 番（加入証明なし）は生成不可の場面なので、生成物を作らない。
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from django.core.files.base import ContentFile
from django.db import models, transaction

from apps.contracts.models import EmploymentContract
from apps.forms_engine.models import GeneratedDocument
from apps.seed.context import DUMMY_PDF
from apps.seed.models import SeedObject
from apps.workers.models import ForeignWorker
from services import (
    contracts,
    form_data_application,
    form_data_contract,
    form_generation,
    form_signatures,
)

AWAITING_BILINGUAL = 2
SIGNED = 26
COLLECTION = 3
WITH_WARNINGS = 15


class DocumentsNotAllowedError(Exception):
    pass


@dataclass
class DocumentsResult:
    documents: list[GeneratedDocument] = field(default_factory=list)
    tasks: int = 0


def dummy_worker(number: int) -> ForeignWorker:
    """seed_dummy の外国人の番号（1 から。作った順）。"""
    ids = SeedObject.objects.filter(content_type__model="foreignworker").order_by("pk")
    pks = [entry.object_id for entry in ids]
    if len(pks) < number:
        raise DocumentsNotAllowedError("Run seed_dummy first.")
    return ForeignWorker.objects.get(pk=pks[number - 1])


def exists() -> bool:
    worker = dummy_worker(AWAITING_BILINGUAL)
    return GeneratedDocument.objects.filter(worker=worker, template__form_id="1-5").exists()


def _contract(worker: ForeignWorker, on: datetime.date) -> EmploymentContract:
    contract = contracts.contract_on(worker, on)
    if contract is None:
        raise DocumentsNotAllowedError(f"No contract for {worker}.")
    return contract


def _generate(
    form_id: str,
    target: models.Model,
    data: Mapping[str, Any],
    on: datetime.date,
    *,
    language: str = "ja",
    accept_warnings: bool = False,
) -> GeneratedDocument:
    document = form_generation.request(
        form_id,
        target=target,
        data=data,
        basis_on=on,
        by=None,
        language=language,
        accept_warnings=accept_warnings,
    )
    form_generation.run(document.pk)  # ジョブを待たずにその場で（ワーカーが後で受けても何もしない）
    document.refresh_from_db()
    return document


@transaction.atomic
def run(today: datetime.date) -> DocumentsResult:
    result = DocumentsResult()
    # 署名待ち・中国語（日中併記の第1-5号）
    worker = dummy_worker(AWAITING_BILINGUAL)
    version = contracts.version_on(_contract(worker, today), today)
    assert version is not None
    result.documents.append(
        _generate("1-5", version, form_data_contract.build_1_5(version), today, language="ja-zh")
    )
    # 署名済み（日本語の第1-5号にスキャンを添付）
    worker = dummy_worker(SIGNED)
    version = contracts.version_on(_contract(worker, today), today)
    assert version is not None
    signed = _generate("1-5", version, form_data_contract.build_1_5(version), today)
    form_signatures.add_signature(
        signed,
        [ContentFile(DUMMY_PDF, name="dummy_signed_1_5.pdf")],
        signed_on=today,
        by=None,
        today=today,
    )
    result.documents.append(signed)
    # 署名待ち・回収のタスク（第1-6号。期限の起算は生成した日。タスクは受付の始まる日で起票する）
    worker = dummy_worker(COLLECTION)
    version = contracts.version_on(_contract(worker, today), today)
    assert version is not None
    awaiting = _generate("1-6", version, form_data_contract.build_1_6(version), today)
    result.documents.append(awaiting)
    opens_on = form_signatures.deadline(today).opens_on or today
    result.tasks = len(form_signatures.raise_collection_tasks(max(opens_on, today)))
    # 警告つき（第13-1号。キャリアアッププラン未交付）
    worker = dummy_worker(WITH_WARNINGS)
    contract = _contract(worker, today)
    data = form_data_application.build(
        "13-1", contract, today, author=form_data_application.default_author("13-1", contract)
    )
    result.documents.append(_generate("13-1", contract, data, today, accept_warnings=True))
    return result
