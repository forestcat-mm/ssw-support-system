"""様式マスタの管理（F13。T53、ADR-0042）。

改正の反映の試し（T53-D4）: 新しい版をリポジトリのデータに足して読み込むだけで、基準日で新旧が
切り替わり、旧版で再生成でき、新しい版を停止すると旧版に戻る（コードは変えない）。
"""

import datetime
import json
import shutil
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError

from apps.core.models import AuditAction, AuditLog
from apps.forms_engine.models import FormTemplate, GeneratedDocument, GenerationStatus
from apps.hosts.models import HostOrganization
from services import (
    form_data_application,
    form_data_periodic,
    form_generation,
    form_master,
    form_templates,
)
from tests.core.factories import make_user, verified_client
from tests.forms_engine.test_form_application import make_case

pytestmark = pytest.mark.django_db

OLD, NEW = "2026-04-01", "2027-04-01"


def add_version(templates: Path, mappings: Path) -> None:
    """第3-6号（別紙2）の新しい版（適用開始日 2027-04-01）を足す（改正を受けた体）。"""
    shutil.copytree(templates / "3-6-2" / OLD, templates / "3-6-2" / NEW)
    manifest_path = templates / "3-6-2" / NEW / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["valid_from"] = NEW
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), "utf-8")
    for suffix in (".json", ".rules.json"):
        data = json.loads((mappings / "3-6-2" / f"{OLD}{suffix}").read_text("utf-8"))
        data["valid_from"] = NEW
        if suffix == ".json":
            data["note"] = "改正の反映の試し（T53-D4）"
        (mappings / "3-6-2" / f"{NEW}{suffix}").write_text(
            json.dumps(data, ensure_ascii=False), "utf-8"
        )


def request(host: HostOrganization, basis_on: datetime.date) -> GeneratedDocument:
    data = form_data_periodic.build_signature(host, basis_on.year - 1)
    return form_generation.request(
        "3-6-2",
        target=host,
        data=data,
        basis_on=basis_on,
        by=None,
    )


def test_a_revision_is_reflected_by_data_only(repo_copy: tuple[Path, Path]) -> None:
    templates, mappings = repo_copy
    form_templates.load_all()
    contract = make_case()
    host = contract.host_organization
    add_version(templates, mappings)
    report = form_templates.load_all()
    assert report.created == ["3-6-2 2027-04-01 ja"]
    before = request(host, datetime.date(2027, 3, 31))
    after = request(host, datetime.date(2028, 3, 31))
    assert (str(before.template.valid_from), str(after.template.valid_from)) == (OLD, NEW)
    assert after.status == GenerationStatus.COMPLETED
    # 旧版で作り直す（生成のときのデータで新しい生成履歴）
    old = FormTemplate.objects.get(form_id="3-6-2", valid_from=OLD)
    again = form_master.regenerate(after, old, by=None)
    assert again.template == old and again.pk != after.pk
    assert again.status == GenerationStatus.COMPLETED
    # 新しい版を停止すると、基準日がその後でも旧版が選ばれる。解除すると戻る
    new = FormTemplate.objects.get(form_id="3-6-2", valid_from=NEW)
    form_master.suspend(new, reason="欄の位置の誤り", by=None)
    assert request(host, datetime.date(2028, 3, 31)).template == old
    assert AuditLog.objects.filter(
        action=AuditAction.UPDATE, object_id=str(new.pk), context__suspended="欄の位置の誤り"
    ).exists()
    form_templates.load_all()  # 読み込み直しても停止は残る
    new.refresh_from_db()
    assert new.suspended_at is not None
    form_master.resume(new, by=None)
    assert request(host, datetime.date(2028, 3, 31)).template == new


def test_the_only_version_cannot_be_suspended(loaded: None) -> None:
    template = FormTemplate.objects.get(form_id="3-6-2")
    with pytest.raises(ValidationError):
        form_master.suspend(template, reason="誤り", by=None)
    with pytest.raises(ValidationError):
        form_master.suspend(template, reason=" ", by=None)


def test_screens(loaded: None) -> None:
    contract = make_case()
    client = verified_client(make_user("kanri-master", roles=("admin",)))
    listing = client.get("/settings/forms/").content.decode()
    template = FormTemplate.objects.get(form_id="3-6-2")
    assert f'data-version="{template.pk}"' in listing
    detail = client.get(f"/settings/forms/{template.pk}/")
    assert template.original_sha256 in detail.content.decode()
    response = client.post(
        f"/settings/forms/{template.pk}/",
        {
            "action": "suspend",
            "reason": "誤り",
            detail.context["once_field"]: detail.context["once_token"],
        },
    )
    assert response.status_code == 200  # 版が 1 つだけなので停止できない（エラーを出す）
    template.refresh_from_db()
    assert template.suspended_at is None
    # 生成物の画面から同じ版で作り直し、記入済み（編集用）の docx をダウンロードできる
    data = form_data_application.build(
        "3-1", contract, datetime.date(2026, 10, 18), author="山田 太郎"
    )
    document = form_generation.request(
        "3-1", target=contract, data=data, basis_on=datetime.date(2026, 10, 18), by=None
    )
    page = client.get(f"/forms/documents/{document.pk}/")
    assert 'id="document-regenerate"' in page.content.decode()
    response = client.post(
        f"/forms/documents/{document.pk}/regenerate/",
        {
            "template": document.template_id,
            page.context["once_field"]: page.context["once_token"],
        },
    )
    assert response.status_code == 302
    assert GeneratedDocument.objects.filter(template=document.template).count() == 2
    download = client.get(f"/forms/documents/{document.pk}/filled/")
    assert download.status_code == 200
    assert b"".join(download.streaming_content)[:2] == b"PK"  # type: ignore[attr-defined]
    nobody = verified_client(make_user("nobody-master"))
    assert nobody.get("/settings/forms/").status_code == 403
