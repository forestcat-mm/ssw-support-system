"""管理画面の整理（T15a）: 見た目のトークン・業務画面へ戻るリンク・開発者向けの項目を隠す。"""

import pytest
from django.test import Client

from tests.core.factories import make_organization

pytestmark = pytest.mark.django_db


def test_admin_uses_business_tokens_and_links_back(admin_client: Client) -> None:
    make_organization(name="テスト登録支援機関")
    html = admin_client.get("/admin/").content.decode()
    assert "/static/ui/tokens.css" in html and "/static/ui/admin-theme.css" in html
    assert "テスト登録支援機関 管理画面" in html
    assert 'class="admin-back-to-app" href="/"' in html


def test_developer_items_are_hidden_from_menu_but_open_by_url(admin_client: Client) -> None:
    index = admin_client.get("/admin/")
    labels = {app["app_label"] for app in index.context["app_list"]}
    assert not labels & {"axes", "django_q", "seed"}
    assert "hosts" in labels
    # 権限は変えない（直接の URL は開ける）
    assert admin_client.get("/admin/django_q/schedule/").status_code == 200
    assert admin_client.get("/admin/axes/accessattempt/").status_code == 200
