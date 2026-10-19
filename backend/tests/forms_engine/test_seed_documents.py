"""ダミーデータの段階3の場面（T39）。

- 31 番（4 社目。加入はあるが構成員証明書が無い）は、第3-1号が「証明書が添付されていません」で
  生成不可。申請の書類の一覧でも、加入の証明書の行が未添付。1〜30 番の場面は変えない。
- seed_documents: 署名待ち（日中併記）・署名済み・回収のタスク・警告つきの生成物。
  2 回目は何もしない。
"""

from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command

from apps.filings.models import Filing, FilingKind
from apps.forms_engine.models import GeneratedDocument, GenerationStatus
from apps.workers.models import ForeignWorker, WorkerStatus
from services import application_sets, contracts, form_data_application, form_generation
from services.form_signatures import SignatureState, state
from tests.forms_engine.conftest import BASE
from tests.forms_engine.test_form_contract import body_texts

pytestmark = pytest.mark.django_db


def test_uncertified_host_blocks_the_pledge(seeded: list[ForeignWorker]) -> None:
    worker = seeded[30]  # 31 番
    assert worker.name_romaji == "NGUYEN VAN AN" and worker.status == WorkerStatus.PRE_ENTRY
    contract = contracts.contract_on(worker, BASE)
    assert contract is not None and "部品工業" in contract.host_organization.name
    data = form_data_application.build(
        "3-1", contract, BASE, author=form_data_application.default_author("3-1", contract)
    )
    result = form_generation.check("3-1", target=contract, data=data, basis_on=BASE)
    assert [finding.code for finding in result.blocking] == ["certificate_missing"]
    rows = {state.row["id"]: state for state in application_sets.overview(worker, "coe", BASE).rows}
    assert rows["coe_ind-3-1"].status == application_sets.Status.MISSING
    assert application_sets.default_set(worker) == "coe"
    # 31 番には支援の記録・届出タスクを作らない（ほかの場面の件数を変えない）
    assert not Filing.objects.filter(worker=worker).exists()


def test_seed_documents_make_the_signature_scenes(
    seeded: list[ForeignWorker], settings: Any
) -> None:
    out = StringIO()
    call_command("seed_documents", stdout=out)
    documents = {
        (document.worker_id, document.template.form_id): document
        for document in GeneratedDocument.objects.select_related("template")
    }
    assert all(d.status == GenerationStatus.COMPLETED for d in documents.values()), [
        d.failure_reason for d in documents.values()
    ]
    bilingual = documents[(seeded[1].pk, "1-5")]
    assert bilingual.template.language == "ja-zh" and state(bilingual) is SignatureState.AWAITING
    # 中国語の行の甲・乙の欄にも名前が入る（修正 1 回目 A7）
    text = "\n".join(body_texts(Path(bilingual.filled_file.path)))
    host_name = bilingual.target.contract.host_organization.name  # type: ignore[union-attr]
    assert any("甲方" in line and host_name in line for line in text.splitlines())
    assert any("乙方" in line and seeded[1].name_romaji in line for line in text.splitlines())
    assert state(documents[(seeded[25].pk, "1-5")]) is SignatureState.SIGNED
    awaiting = documents[(seeded[2].pk, "1-6")]
    assert state(awaiting) is SignatureState.AWAITING and awaiting.template.language == "ja"
    warned = documents[(seeded[14].pk, "13-1")]
    assert [f["code"] for f in warned.findings] == ["career_plan_missing"]
    tasks = Filing.objects.filter(kind=FilingKind.SIGNED_COPY_COLLECTION)
    assert {task.object_id for task in tasks} == {bilingual.pk, awaiting.pk}
    count = GeneratedDocument.objects.count()
    call_command("seed_documents", stdout=out)
    assert GeneratedDocument.objects.count() == count
    assert "already exist" in out.getvalue()
