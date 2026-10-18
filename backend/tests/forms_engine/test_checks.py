"""帳票の生成前の検査（T30、ADR-0019）。

- ダミーデータの場面どおりに「生成不可」「警告」が出て、不足ごとの行き先の画面が開ける。
- 規則のデータ（.rules.json）を替えると、コードを変えずに判定が変わる。
- 規則のデータの不備は読み込みで止める。
"""

import datetime
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from django.urls import resolve

from apps.core.models import AttachmentKind, AuditAction, AuditLog
from apps.forms_engine.models import FormMapping, GeneratedDocument
from apps.hosts.models import HostMembership
from apps.support.models import DeliveryKind
from apps.workers.models import ForeignWorker
from services import (
    attachments,
    contracts,
    form_checks,
    form_generation,
    form_samples,
    form_templates,
)
from tests.core.factories import make_user, verified_client

pytestmark = pytest.mark.django_db

BASE = datetime.date(2026, 10, 16)

# 1-17・5-9 の見本の規則で、ダミーデータ（基準日 BASE）に出る不足（外国人の番号 → (規則, コード)）
EXPECTED_1_17 = {
    1: {("residence", "residence_expired")},  # 在留期限が D−5
    4: {("contract", "contract_missing")},  # 自己都合で離職（D−4）
    6: {("contract", "contract_missing")},  # 会社都合で離職（D−9）
    9: {("career_plan", "career_plan_missing")},  # 転籍の後の契約（水産加工）が未交付
    15: {("career_plan", "career_plan_missing")},  # 食品の契約が未交付
}
EXPECTED_5_9 = {
    10: {("briefing_warnings", "below_guideline")},  # 2 時間（署名なしは生成の後なので出さない）
    25: set(),  # 完全・署名済み
}
BLOCKING_CODES = {"residence_expired", "contract_missing", "briefing_missing"}


def check(form_id: str, worker: ForeignWorker, on: datetime.date = BASE) -> form_checks.CheckResult:
    target, data = form_samples.sample(form_id, worker, on)
    return form_generation.check(form_id, target=target, data=data, basis_on=on)


def found(result: form_checks.CheckResult) -> set[tuple[str, str]]:
    return {(finding.rule, finding.code) for finding in result.findings}


def test_dummy_data_scenes_block_or_warn_as_intended(seeded: list[ForeignWorker]) -> None:
    for number, worker in enumerate(seeded, start=1):
        result = check("1-17", worker)
        assert found(result) == EXPECTED_1_17.get(number, set()), number
        assert all(f.code in BLOCKING_CODES for f in result.blocking), number

        result = check("5-9", worker)
        # 事前ガイダンスの記録が無い人には「記録がありません」の 1 件だけ（欄ごとの不足を重ねない）
        expected = EXPECTED_5_9.get(number, {("briefing_record", "briefing_missing")})
        assert found(result) == expected, number
        if expected == {("briefing_record", "briefing_missing")}:
            assert len(result.findings) == 1 and result.blocking[0].message == (
                "事前ガイダンスの記録がありません。"
            )


def test_each_finding_leads_to_the_screen_that_fixes_it(seeded: list[ForeignWorker]) -> None:
    """行き先の URL の名前・対象の ID が正しく、管理者でその画面が開ける。"""
    client = verified_client(make_user("kanri", roles=("admin",)))
    worker = {number: seeded[number - 1] for number in (1, 4, 10, 15, 2)}
    cases: list[tuple[form_checks.Finding, str, dict[str, Any]]] = []
    for number, form_id in ((1, "1-17"), (4, "1-17"), (15, "1-17"), (10, "5-9"), (2, "5-9")):
        for finding in check(form_id, worker[number]).findings:
            cases.append((finding, form_id, {"number": number}))
    expected = {
        "residence_expired": ("workers:history", {"pk": worker[1].pk}),
        "contract_missing": ("workers:contracts", {"pk": worker[4].pk}),
        "career_plan_missing": ("workers:support_deliveries", {"pk": worker[15].pk}),
        "below_guideline": (
            "workers:support_briefing",
            {
                "pk": worker[10].pk,
                "briefing_id": form_samples.latest_briefing(worker[10], "pre_guidance").pk,  # type: ignore[arg-type, union-attr]
            },
        ),
        "briefing_missing": (
            "workers:support_briefing_create",
            {"pk": worker[2].pk, "kind": "pre_guidance"},
        ),
    }
    assert {finding.code for finding, _form, _n in cases} == set(expected)
    for finding, _form, _n in cases:
        assert finding.destination is not None
        url_name, kwargs = expected[finding.code]
        assert (finding.destination.url_name, dict(finding.destination.kwargs)) == (
            url_name,
            kwargs,
        )
        match = resolve(finding.destination.url())
        assert f"{match.namespace}:{match.url_name}" == url_name
        assert client.get(finding.destination.url()).status_code == 200, finding.code


def _food_membership(worker: ForeignWorker) -> HostMembership:
    contract = contracts.contract_on(worker, BASE)
    assert contract is not None
    version = contracts.version_on(contract, BASE)
    assert version is not None and version.site is not None
    return contract.host_organization.memberships.get(field=version.site.field)


def test_membership_certificate_missing_or_expired_blocks(seeded: list[ForeignWorker]) -> None:
    """加入証明が無い・有効期限を過ぎた証明書（無いものとみなす）→ 生成不可（ADR-0004・0019）。"""
    worker = seeded[14]  # 15 番（食品の会社。加入の有効期限は D+25）
    membership = _food_membership(worker)
    host_url = ("hosts:detail", {"pk": membership.host_organization_id})

    expired = check("1-17", worker, BASE + datetime.timedelta(days=26))
    assert ("membership", "membership_expired") in found(expired)
    finding = next(f for f in expired.blocking if f.code == "membership_expired")
    assert (finding.destination.url_name, dict(finding.destination.kwargs)) == host_url  # type: ignore[union-attr]
    on_last_day = check("1-17", worker, BASE + datetime.timedelta(days=25))
    assert ("membership", "membership_expired") not in found(on_last_day)

    # 有効期限が空の加入（水産加工）は期限切れにしない
    later = check("1-17", seeded[8], BASE + datetime.timedelta(days=400))  # 9 番
    assert not any(f.code.startswith("membership") for f in later.findings)

    for certificate in membership.attachments.filter(kind=AttachmentKind.MEMBERSHIP_CERTIFICATE):
        attachments.void_attachment(certificate, by=None, reason="テスト")
    result = check("1-17", worker)
    assert ("membership", "certificate_missing") in found(result)
    assert result.blocking[0].message.startswith(membership.host_organization.name)


def test_staff_without_the_support_language_warns_about_interpreters(
    seeded: list[ForeignWorker],
) -> None:
    worker = seeded[1]  # 2 番（中国語。担当は鈴木）
    staff = worker.assigned_staff
    assert staff is not None
    staff.languages.remove(worker.support_language)
    result = check("1-17", worker)
    finding = next(f for f in result.warnings if f.code == "staff_language")
    assert "通訳の手配がある場合は問題ありません" in finding.message
    assert finding.destination is not None
    assert finding.destination.url() == f"/settings/staff/{staff.pk}/"


def test_delivery_warnings_point_at_the_delivery(seeded: list[ForeignWorker]) -> None:
    worker = seeded[0]  # 1 番（支援計画書の写し。署名済みの控えつき）
    rules = [
        {
            "id": "copy",
            "type": "check",
            "name": "delivery_warnings",
            "params": {"kind": DeliveryKind.SUPPORT_PLAN_COPY.value},
            "severity": "warn",
        }
    ]
    context = form_checks.CheckContext(worker=worker, target=worker, basis_on=BASE, data={})
    assert form_checks.evaluate(rules, context).findings == []
    delivery = worker.document_deliveries.get(kind=DeliveryKind.SUPPORT_PLAN_COPY)
    for receipt in delivery.attachments.all():
        attachments.void_attachment(receipt, by=None, reason="テスト")
    [finding] = form_checks.evaluate(rules, context).warnings
    assert finding.code == "no_receipt"
    assert finding.destination is not None
    assert dict(finding.destination.kwargs) == {"pk": worker.pk, "delivery_id": delivery.pk}


# --- 規則のデータを替える -------------------------------------------------------------


def _edit_rules(mappings: Path, form_id: str, edit: Callable[[list[dict[str, Any]]], None]) -> None:
    path = mappings / form_id / "2026-10-17.rules.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    edit(data["rules"])
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_changing_the_rules_data_changes_the_result_without_code(
    repo_copy: tuple[Path, Path], seeded: list[ForeignWorker]
) -> None:
    _templates, mappings = repo_copy
    worker_1, worker_15 = seeded[0], seeded[14]
    mapping = FormMapping.objects.get(template__form_id="1-17")
    # 生成履歴がある版でも、規則のファイルは置き換えられる
    GeneratedDocument.objects.create(
        template=mapping.template, target=worker_1, worker=worker_1, basis_on=BASE, snapshot="{}"
    )
    before = mapping.rules_sha256

    def edit(rules: list[dict[str, Any]]) -> None:
        for rule in rules:
            if rule["id"] == "career_plan":
                rule["severity"] = "block"  # 警告 → 生成不可
        rules[:] = [rule for rule in rules if rule["id"] != "residence"]  # 在留期限を見ない

    _edit_rules(mappings, "1-17", edit)
    report = form_templates.load_all()
    assert report.rules_updated == ["1-17 2026-10-17 ja"] and report.updated == []
    mapping.refresh_from_db()
    assert mapping.rules_sha256 != before
    assert not check("1-17", worker_1).findings
    assert [f.code for f in check("1-17", worker_15).blocking] == ["career_plan_missing"]
    log = AuditLog.objects.filter(
        action=AuditAction.UPDATE, content_type__model="formmapping", object_id=str(mapping.pk)
    ).get()
    assert log.changes["rules_sha256"] == [before, mapping.rules_sha256]


def test_generated_document_keeps_the_rules_sha_and_the_accepted_warnings(
    seeded: list[ForeignWorker], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(form_generation, "run", lambda document_id: None)  # 変換はしない
    monkeypatch.setattr("services.jobs.enqueue", lambda *args, **kwargs: None)
    worker_1, worker_15 = seeded[0], seeded[14]
    target, data = form_samples.sample("1-17", worker_1, BASE)
    with pytest.raises(form_generation.GenerationBlocked) as blocked:
        form_generation.request("1-17", target=target, data=data, basis_on=BASE, by=None)
    assert [f.code for f in blocked.value.result.blocking] == ["residence_expired"]
    assert not GeneratedDocument.objects.exists()  # 生成不可なら履歴を作らない

    target, data = form_samples.sample("1-17", worker_15, BASE)
    with pytest.raises(form_generation.WarningsNotAccepted):
        form_generation.request("1-17", target=target, data=data, basis_on=BASE, by=None)
    assert not GeneratedDocument.objects.exists()
    document = form_generation.request(
        "1-17", target=target, data=data, basis_on=BASE, by=None, accept_warnings=True
    )
    mapping = FormMapping.objects.get(template__form_id="1-17")
    assert document.rules_sha256 == mapping.rules_sha256 != ""
    assert [item["code"] for item in document.findings] == ["career_plan_missing"]
    assert document.findings[0]["destination"] == {
        "url_name": "workers:support_deliveries",
        "kwargs": {"pk": worker_15.pk},
    }
    assert worker_15.name_romaji not in json.dumps(document.findings)  # データの値は入れない


# --- 規則のデータの不備 -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda r: r.append({"id": "x", "type": "regex", "severity": "warn"}), "unknown type"),
        (
            lambda r: r.append({"id": "x", "type": "check", "name": "nope", "severity": "warn"}),
            "unknown check",
        ),
        (
            lambda r: r.append(
                {
                    "id": "x",
                    "type": "required",
                    "key": "worker.unknown",
                    "label": "x",
                    "severity": "block",
                }
            ),
            "not in the fields",
        ),
        (
            lambda r: r.append(
                {"id": "x", "type": "check", "name": "residence_valid_on_basis", "severity": "stop"}
            ),
            "severity",
        ),
        (
            lambda r: r.insert(
                0,
                {
                    "id": "x",
                    "type": "check",
                    "name": "residence_valid_on_basis",
                    "severity": "warn",
                    "requires": "contract",
                },
            ),
            "earlier rule",
        ),
        (
            lambda r: r.append(
                {
                    "id": "x",
                    "type": "check",
                    "name": "residence_valid_on_basis",
                    "severity": "warn",
                    "params": {"a": 1},
                }
            ),
            "unknown params",
        ),
        (lambda r: r.append(dict(r[0])), "duplicate id"),
        (
            lambda r: r.append(
                {
                    "id": "x",
                    "type": "required",
                    "key": "worker.name_romaji",
                    "label": "x",
                    "severity": "block",
                    "fix": {"object": "worker", "url": "workers:nope"},
                }
            ),
            "does not resolve",
        ),
        (
            lambda r: r.append(
                {
                    "id": "x",
                    "type": "max_length",
                    "key": "worker.name_romaji",
                    "label": "x",
                    "severity": "warn",
                    "max_length": 0,
                }
            ),
            "max_length",
        ),
    ],
)
def test_broken_rules_stop_loading(
    repo_copy: tuple[Path, Path], edit: Callable[[list[dict[str, Any]]], None], message: str
) -> None:
    _templates, mappings = repo_copy
    _edit_rules(mappings, "1-17", edit)
    with pytest.raises(form_templates.FormTemplateError, match=message):
        form_templates.load_all()


def test_rules_file_is_required_and_rules_do_not_belong_in_the_mapping(
    repo_copy: tuple[Path, Path],
) -> None:
    _templates, mappings = repo_copy
    rules_path = mappings / "5-9" / "2026-10-17.rules.json"
    saved = rules_path.read_text(encoding="utf-8")
    rules_path.unlink()
    with pytest.raises(form_templates.FormTemplateError, match="not found"):
        form_templates.load_all()
    rules_path.write_text(saved, encoding="utf-8")
    mapping_path = mappings / "5-9" / "2026-10-17.json"
    definition = json.loads(mapping_path.read_text(encoding="utf-8"))
    definition["rules"] = json.loads(saved)["rules"]
    mapping_path.write_text(json.dumps(definition, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(form_templates.FormTemplateError, match="rules belong in the rules file"):
        form_templates.load_all()


# --- データの規則 ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rule", "value", "message"),
    [
        ({"type": "required"}, "", "「氏名」が空です。"),
        ({"type": "required"}, "  ", "「氏名」が空です。"),
        ({"type": "required"}, "WANG", None),
        ({"type": "date"}, "2026-02-30", "「氏名」が日付ではありません。"),
        ({"type": "date"}, datetime.date(2026, 2, 28), None),
        ({"type": "date"}, "", None),  # 空は required だけが見る
        ({"type": "width", "width": "half"}, "WANG FANG", None),
        ({"type": "width", "width": "half"}, "ＷＡＮＧ", "「氏名」は半角で書いてください。"),
        ({"type": "width", "width": "full"}, "王　芳", None),
        ({"type": "width", "width": "full"}, "王 芳", "「氏名」は全角で書いてください。"),
        ({"type": "max_length", "max_length": 4}, "王芳王芳", None),
        ({"type": "max_length", "max_length": 4}, "WANGF", "「氏名」が 4 文字を超えています。"),
    ],
)
def test_field_rules(rule: dict[str, Any], value: Any, message: str | None) -> None:
    assert form_checks._field_issue(rule | {"label": "氏名"}, value) == message


def test_every_rule_file_in_the_repository_loads(loaded: None) -> None:
    for mapping in FormMapping.objects.all():
        assert mapping.rules and mapping.rules_sha256 and mapping.rules_path.endswith(".rules.json")


def test_contract_on_the_basis_date(seeded: list[ForeignWorker]) -> None:
    """基準日の契約: 始まっていて終了していない契約。無ければ、その後に始まる予定の契約。"""
    assert contracts.contract_on(seeded[3], BASE) is None  # 4 番（D−4 に終了）
    assert contracts.contract_on(seeded[3], BASE - datetime.timedelta(days=4)) is not None
    transferred = contracts.contract_on(seeded[8], BASE)  # 9 番（転籍の後の水産加工）
    assert transferred is not None
    assert transferred.current_starts_on == BASE - datetime.timedelta(days=59)
    earlier = contracts.contract_on(seeded[8], BASE - datetime.timedelta(days=61))
    assert earlier is not None and earlier.pk != transferred.pk  # 転籍の前の食品の契約
    planned = contracts.contract_on(seeded[24], BASE)  # 25 番（入国前）
    assert planned is not None and planned.current_starts_on > BASE
    version = contracts.version_on(transferred, BASE)
    assert version is not None and version.contract_id == transferred.pk
