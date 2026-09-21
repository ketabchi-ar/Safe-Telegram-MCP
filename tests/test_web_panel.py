"""Test the Web Dashboard API routes."""

import pytest
from starlette.testclient import TestClient
from telegram_mcp.web_panel import app
from telegram_mcp.security import SafeSecurityConfig, update_security_config


def test_dashboard_and_api(tmp_path):
    cfg_file = tmp_path / "safe_config.json"
    update_security_config(SafeSecurityConfig(), cfg_file)

    client = TestClient(app)

    # 1. Test HTML Dashboard
    res = client.get("/")
    assert res.status_code == 200
    assert "Safe-Telegram-MCP" in res.text
    assert "Allowed Chats" in res.text

    # 2. Test Get Config
    res = client.get("/api/config")
    assert res.status_code == 200
    data = res.json()
    assert "safe_mode" in data
    assert "allowed_chats" in data

    # 3. Test Whitelist Add
    res = client.post("/api/whitelist/add", json={"chat": "@testgroup"})
    assert res.status_code == 200
    data = res.json()
    assert "@testgroup" in data["allowed_chats"]

    # 4. Test Whitelist Remove
    res = client.post("/api/whitelist/remove", json={"chat": "@testgroup"})
    assert res.status_code == 200
    data = res.json()
    assert "@testgroup" not in data["allowed_chats"]

    # 5. Test Update Config
    res = client.post("/api/config", json={"safe_mode": True, "read_only": True, "rate_limit_delay": 2.0})
    assert res.status_code == 200
    res = client.get("/api/config")
    assert res.json()["read_only"] is True
    assert res.json()["rate_limit_delay"] == 2.0

    # 6. Test Whitelist Export
    res = client.get("/api/whitelist/export")
    assert res.status_code == 200
    assert "attachment" in res.headers.get("content-disposition", "")
    assert "allowed_chats" in res.json()

    # 7. Test Whitelist Import
    res = client.post("/api/whitelist/import", json={"allowed_chats": ["@imported_chat"]})
    assert res.status_code == 200
    assert "@imported_chat" in res.json()["allowed_chats"]
