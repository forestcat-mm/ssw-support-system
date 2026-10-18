"""定期面談報告書の回答（T35、ADR-0024）: 必須・⑥と４・第4-3号・T35 より前の記録。"""

import io
from dataclasses import replace
from datetime import date
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import Client

from apps.filings.models import Filing, FilingKind, FilingStatus
from apps.support.models import (
    InterviewAnswer,
    InterviewAnswerSet,
    InterviewNonconformity,
    PeriodicInterview,
)
from apps.workers.models import WorkerStatus
from services import incident_reports, interview_answers, support_interviews
from services.interview_answers import AnswerInput, AnswerSetInput, NonconformityInput
from tests.contracts.factories import make_contract, site_with_categories
from tests.core.factories import make_staff
from tests.support.interview_helpers import answered, nonconformity_for
from tests.support.test_periodic_interviews import TODAY, interview, post_data, row
from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db

ON = date(2026, 7, 1)


@pytest.fixture
def worker() -> Any:
    worker = make_worker(assigned_staff=make_staff(name="佐藤"), status=WorkerStatus.RESIDING)
    make_contract(worker, site=site_with_categories())
    return worker


def errors_of(worker: Any, data: Any) -> set[str]:
    with pytest.raises(ValidationError) as error:
        support_interviews.create_interview(worker, data, by=None, today=TODAY)
    return set(error.value.error_dict)


def test_questions_are_the_18_rows_of_both_forms() -> None:
    version = interview_answers.version_for(ON)
    assert len(version.codes) == 18 and version.codes[0] == "1.1" and version.codes[-1] == "5.2"
    by_code = {q.code: q for q in version.questions}
    assert by_code["4.2"].self_text == "健康状態に異常がないこと。"
    assert by_code["4.2"].supervisor_text.startswith("健康診断を定期的に実施し")
    assert by_code["5.2"].other


# --- 保存と必須（T35-D2） -----------------------------------------------------------------


def test_answers_are_saved_per_row_and_question(worker: Any) -> None:
    answers = interview_answers.all_no(ON)
    answers["2.4"] = AnswerInput(True, "寮の窓が壊れている。")
    answers["5.2"] = AnswerInput(True, "近隣とのトラブル。")
    data = interview(
        worker,
        self_answers=AnswerSetInput(answers=answers, other_subject="近隣", notable="なし"),
        supervisor_answers=AnswerSetInput(
            answers=interview_answers.all_no(ON),
            supervisor_position="工場長",
            supervisor_department="製造部",
        ),
    )
    saved = support_interviews.create_interview(worker, data, by=None, today=TODAY)
    self_record = saved.records.get(counterpart="worker")
    answer_set = InterviewAnswerSet.objects.get(record=self_record)
    assert answer_set.question_version == interview_answers.version_for(ON).id
    assert answer_set.answers.count() == 18
    assert list(answer_set.answers.filter(has_problem=True).values_list("code", "detail")) == [
        ("2.4", "寮の窓が壊れている。"),
        ("5.2", "近隣とのトラブル。"),
    ]
    supervisor = InterviewAnswerSet.objects.get(record__counterpart="supervisor")
    assert (supervisor.supervisor_position, supervisor.supervisor_department) == (
        "工場長",
        "製造部",
    )
    assert InterviewNonconformity.objects.get(interview=saved).exists is False
    assert interview_answers.problems(saved) == 2


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("missing", {"self.answer.1.1"}),
        ("no_detail", {"self.detail.2.1"}),
        ("other_subject", {"self.other_subject"}),
        ("online", {"supervisor.recording_location"}),
        ("no_nonconformity", {"nonconformity.exists"}),
    ],
)
def test_answers_are_required(worker: Any, change: str, expected: set[str]) -> None:
    data = interview(worker)
    answers = dict(data.self_answers.answers)  # type: ignore[union-attr]
    if change == "missing":
        del answers["1.1"]
    elif change == "no_detail":
        answers["2.1"] = AnswerInput(True, "")
    elif change == "other_subject":
        answers["5.2"] = AnswerInput(True, "内容")
    if change in ("missing", "no_detail", "other_subject"):
        data = replace(data, self_answers=AnswerSetInput(answers=answers))
    elif change == "online":
        data = replace(
            data,
            supervisor_row=row(
                worker, ON, counterpart_name="工場長 田中", method="online", meeting_url=""
            ),
        )
    else:
        data = replace(data, nonconformity=NonconformityInput())
    assert expected <= errors_of(worker, data)
    assert not PeriodicInterview.objects.exists()


def test_violation_needs_nonconformity_and_its_details(worker: Any) -> None:
    """労働法令違反の疑いが「あり」なら ⑥ は「有り」、「有り」なら ４ を書く（T35-D4）。"""
    data = interview(
        worker,
        violation_suspected=True,
        report_required=False,
        nonconformity=NonconformityInput(exists=False),
    )
    assert "nonconformity.exists" in errors_of(worker, data)
    data = replace(data, nonconformity=NonconformityInput(exists=True))
    assert {
        "nonconformity.occurred_on",
        "nonconformity.content",
        "nonconformity.worker_guidance",
        "nonconformity.host_notified",
        "nonconformity.immigration_guided",
        "nonconformity.unreported_reason",
    } <= errors_of(worker, data)
    # ⑥ が「なし」なら ４ は書かない
    data = interview(worker, nonconformity=NonconformityInput(exists=False, content="何か"))
    assert "nonconformity.exists" in errors_of(worker, data)


def test_records_without_answers_only_for_dummy_data(worker: Any) -> None:
    """T35 より前の記録と同じ形（回答なし）は without_answers でだけ作れる。回答を渡すとエラー。"""
    bare = support_interviews.InterviewInput(
        self_row=row(worker, ON), supervisor_row=row(worker, ON, counterpart_name="工場長 田中")
    )
    saved = support_interviews.create_interview(
        worker, bare, by=None, today=TODAY, without_answers=True
    )
    assert not interview_answers.has_answers(saved)
    assert interview_answers.nonconformity_of(saved) is None
    with pytest.raises(ValidationError):
        support_interviews.create_interview(
            worker, answered(bare), by=None, today=TODAY, without_answers=True
        )


# --- 第4-3号（T35-D5） ---------------------------------------------------------------------


def difficulty_tasks(worker: Any) -> list[Filing]:
    return list(Filing.objects.filter(kind=FilingKind.SUPPORT_DIFFICULTY, worker=worker))


def test_nonconformity_raises_a_report_task_on_the_interview_day(worker: Any) -> None:
    found = nonconformity_for(replace(interview(worker), violation_suspected=True), ON)
    data = interview(worker, nonconformity=found)
    saved = support_interviews.create_interview(worker, data, by=None, today=TODAY)
    [task] = difficulty_tasks(worker)
    assert task.basis_on == ON
    assert task.trigger_details["sources"] == [incident_reports.nonconformity_key(saved)]


def test_report_and_nonconformity_of_one_pair_are_one_task(worker: Any) -> None:
    """同じ組の「通報が要」と ⑥ は同じ事由として 1 件（日は通報日）。取消で両方の参照が外れる。"""
    data = interview(
        worker,
        violation_suspected=True,
        report_required=True,
        report_to="labor_standards_office",
        reported_on=date(2026, 7, 3),
    )
    saved = support_interviews.create_interview(worker, data, by=None, today=TODAY)
    [task] = difficulty_tasks(worker)
    assert task.basis_on == date(2026, 7, 3)
    assert task.trigger_details["sources"] == incident_reports.interview_keys(saved)
    result = support_interviews.void_interview(saved, by=None, reason="別の人の面談")
    task.refresh_from_db()
    assert task.status == FilingStatus.CANCELLED and task.trigger_details["sources"] == []
    assert result.incident.cancelled == [task]


# --- 画面 -----------------------------------------------------------------------------------


def test_screen_records_answers_and_shows_them(support_staff_client: Client, worker: Any) -> None:
    url = f"/workers/{worker.pk}/support/interviews/new/"
    page = support_staff_client.get(url)
    html = page.content.decode()
    assert (
        'name="self_answers-answer_1_1"' in html and 'name="supervisor_answers-answer_5_2"' in html
    )
    assert 'data-set-radios="no"' in html  # すべて「問題無」にする
    assert "健康診断を定期的に実施し" in html  # 監督者用の文言（第5-6号）
    assert 'data-enabled-when="self_answers-answer_2_1=yes"' in html
    data = post_data(
        page,
        **{
            "self_answers-answer_2_1": "yes",
            "self_answers-detail_2_1": "残業代が少ない。",
            "supervisor_answers-supervisor_position": "工場長",
        },
    )
    response = support_staff_client.post(url, data)
    assert PeriodicInterview.objects.filter(worker=worker).exists()
    shown = support_staff_client.get(response["Location"]).content.decode()
    assert "残業代が少ない。" in shown and 'data-answers="worker"' in shown
    assert 'id="interview-generate"' in shown


def test_screen_requires_answers(support_staff_client: Client, worker: Any) -> None:
    url = f"/workers/{worker.pk}/support/interviews/new/"
    page = support_staff_client.get(url)
    data = post_data(page)
    del data["self_answers-answer_1_1"]
    data["supervisor_answers-answer_3_2"] = "yes"
    response = support_staff_client.post(url, data)
    html = response.content.decode()
    assert response.status_code == 200
    assert 'id="id_self_answers-answer_1_1-error"' in html
    assert 'id="id_supervisor_answers-detail_3_2-error"' in html
    assert not PeriodicInterview.objects.exists()


def test_old_records_without_answers_still_work(support_staff_client: Client, worker: Any) -> None:
    """T35 より前の記録: 詳細は 200 で「回答はありません」、取消とタスクの巻き戻しは今のとおり。"""
    bare = support_interviews.InterviewInput(
        self_row=row(worker, ON), supervisor_row=row(worker, ON, counterpart_name="工場長 田中")
    )
    saved = support_interviews.create_interview(
        worker, bare, by=None, today=TODAY, without_answers=True
    )
    detail = support_staff_client.get(f"/workers/{worker.pk}/support/interviews/{saved.pk}/")
    assert detail.status_code == 200
    assert detail.content.decode().count("質問の回答はありません（T35 より前の記録）") == 3
    result = support_interviews.void_interview(saved, by=None, reason="誤り")
    assert result.tasks_reverted
    assert not InterviewAnswer.objects.exists()


def test_task_detail_links_the_interview_and_its_reports(
    support_staff_client: Client, worker: Any
) -> None:
    """第4-3号のタスクから元の面談の組へ辿れる（報告書は手で紐づける。T35-D6）。"""
    data = interview(
        worker,
        violation_suspected=True,
        report_required=True,
        report_to="labor_standards_office",
        reported_on=date(2026, 7, 3),
    )
    saved = support_interviews.create_interview(worker, data, by=None, today=TODAY)
    [task] = difficulty_tasks(worker)
    html = support_staff_client.get(f"/filings/{task.pk}/").content.decode()
    assert f'data-interview-source="{saved.pk}"' in html
    assert f"/support/interviews/{saved.pk}/" in html
    assert "この面談の報告書はまだ生成されていません" in html


# --- 移行（既存の表を変えない） --------------------------------------------------------------


def test_migration_only_creates_the_new_tables() -> None:
    """support 0010 の SQL は新しい 3 表の CREATE と、その表への制約だけ（既存の表は変えない）。"""
    out = io.StringIO()
    call_command("sqlmigrate", "support", "0010", stdout=out)
    statements = [
        line for line in out.getvalue().splitlines() if line and not line.startswith("--")
    ]
    new_tables = (
        '"support_interviewanswerset"',
        '"support_interviewanswer"',
        '"support_interviewnonconformity"',
    )
    for statement in statements:
        if statement.startswith(("BEGIN", "COMMIT")):
            continue
        assert statement.startswith(("CREATE TABLE", "CREATE INDEX", "ALTER TABLE")), statement
        if statement.startswith("ALTER TABLE"):
            assert statement.split()[2] in new_tables, statement
    assert "UPDATE" not in out.getvalue() and "DROP" not in out.getvalue()
