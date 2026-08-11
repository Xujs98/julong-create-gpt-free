# -*- coding: utf-8 -*-
from unittest.mock import patch
from pathlib import Path

from webui.app import create_app


ROOT = Path(__file__).parents[1]


def _client():
    client = create_app(auth_code="test-auth").test_client()
    client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
    return client


def test_account_groups_api_lists_and_creates_groups():
    client = _client()
    groups = [{"id": "all", "name": "全部", "count": 2, "is_all": True}]
    with patch("webui.app.db.list_account_groups", return_value=groups), patch(
        "webui.app.db.create_account_group", return_value={"id": 2, "name": "新组", "count": 0}
    ) as create:
        response = client.get("/api/account-groups")
        assert response.status_code == 200
        assert response.get_json()["groups"] == groups
        response = client.post("/api/account-groups", json={"name": "新组"})
        assert response.status_code == 201
        create.assert_called_once_with("新组")


def test_accounts_api_passes_group_filter_to_page_query():
    client = _client()
    with patch(
        "webui.app.db.list_accounts_page",
        return_value={"items": [{"id": 1, "email": "a@example.com", "group_name": "新组"}], "total": 1},
    ) as list_page:
        response = client.get("/api/accounts?paged=1&page=1&page_size=20&group=%E6%96%B0%E7%BB%84")
    assert response.status_code == 200
    assert response.get_json()["items"][0]["group_name"] == "新组"
    assert list_page.call_args.kwargs["group_filter"] == "新组"


def test_account_group_move_api_validates_target_and_returns_updates():
    client = _client()
    with patch(
        "webui.app.db.move_accounts_to_group",
        return_value=([{"id": 1, "group_name": "新组"}], []),
    ) as move:
        response = client.post("/api/accounts/group-move", json={"account_ids": [1], "group_id": 2})
    assert response.status_code == 200
    assert response.get_json()["updated_count"] == 1
    move.assert_called_once_with([1], 2)


def test_account_group_filter_is_persisted_and_restored_in_modern_ui():
    """账号页应保存分组选择，并在刷新后恢复有效分组。"""
    source = (ROOT / "webui/templates/index.html").read_text(encoding="utf-8")
    assert "gpt_console_account_group_filter_v2" in source
    assert "function saveAccountGroupFilter()" in source
    assert "function restoreAccountGroupFilter(groups)" in source
    assert "localStorage.setItem(ACCOUNT_GROUP_FILTER_STORAGE_KEY" in source
    assert "restoreAccountGroupFilter(ACCOUNT_GROUPS)" in source
    assert "saveAccountGroupFilter();" in source
