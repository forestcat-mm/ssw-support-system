"""ダミーデータの段階3の場面（T39）。

- 31 番（4 社目。加入はあるが構成員証明書が無い）は、第3-1号が「証明書が添付されていません」で
  生成不可。申請の書類の一覧でも、加入の証明書の行が未添付。1〜30 番の場面は変えない。
- seed_documents: 署名待ち（日中併記）・署名済み・回収のタスク・警告つきの生成物。
  2 回目は何もしない。
"""

import datetime
from collections.abc import Callable
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from apps.filings.models import Filing, FilingKind
from apps.forms_engine.models import GeneratedDocument, GenerationStatus
from apps.seed.models import SeedRun
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
    seeded: list[ForeignWorker], settings: Any, today_is: Callable[[datetime.date], None]
) -> None:
    # seed_dummy の 60 日後に流しても、記録した seed_dummy の基準日で作る（fix2）
    today_is(BASE + datetime.timedelta(days=60))
    out = StringIO()
    call_command("seed_documents", stdout=out)
    assert f"base date {BASE}" in out.getvalue()
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
    # 生成日・署名日は基準日（seed の中だけ「今」をずらす）
    assert {timezone.localdate(d.generated_at) for d in documents.values() if d.generated_at} == {
        BASE
    }
    count = GeneratedDocument.objects.count()
    call_command("seed_documents", stdout=out)
    assert GeneratedDocument.objects.count() == count
    assert "already exist" in out.getvalue()


def test_seed_documents_date_option_overrides_the_recorded_base(
    seeded: list[ForeignWorker],
) -> None:
    """--date（make seed-docs DATE=…）は、記録した seed_dummy の基準日より優先する。"""
    on = BASE + datetime.timedelta(days=3)
    out = StringIO()
    call_command("seed_documents", "--date", on.isoformat(), stdout=out)
    assert f"base date {on}" in out.getvalue()
    dates = {timezone.localdate(d.generated_at) for d in GeneratedDocument.objects.all()}
    assert dates == {on}


def test_seed_documents_uses_today_without_the_record(
    seeded: list[ForeignWorker], today_is: Callable[[datetime.date], None]
) -> None:
    """基準日の記録の無い古い開発用 DB は今日で作り、DATE で渡すよう知らせる。"""
    SeedRun.objects.all().delete()
    today_is(BASE)
    out = StringIO()
    call_command("seed_documents", stdout=out)
    assert "基準日が記録されていない" in out.getvalue() and f"base date {BASE}" in out.getvalue()


@pytest.mark.parametrize(("debug", "allowed"), [(False, True), (True, False)])
def test_seed_documents_refuses_outside_development(
    seeded: list[ForeignWorker], settings: Any, debug: bool, allowed: bool
) -> None:
    """DEBUG か ALLOW_DUMMY_DATA が false なら、何も作らずに止まる（本番の設定）。"""
    settings.DEBUG = debug
    settings.ALLOW_DUMMY_DATA = allowed
    with pytest.raises(CommandError):
        call_command("seed_documents", stdout=StringIO())
    assert not GeneratedDocument.objects.exists()
