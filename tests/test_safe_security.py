"""Tests for Safe-Telegram-MCP security layer."""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from telegram_mcp.security import (
    SafeSecurityConfig,
    is_chat_allowed,
    update_security_config,
    SafeSecurityMiddleware,
    enforce_rate_limit,
)
from mcp.types import CallToolResult, TextContent


def test_whitelist_matching(tmp_path):
    cfg_file = tmp_path / "safe_config.json"
    cfg = SafeSecurityConfig(
        safe_mode=True,
        allowed_chats=["me", "@mychannel", "987654321"],
    )
    update_security_config(cfg, cfg_file)

    assert is_chat_allowed("me") is True
    assert is_chat_allowed("self") is True
    assert is_chat_allowed("@mychannel") is True
    assert is_chat_allowed("mychannel") is True  # case-insensitive and without @
    assert is_chat_allowed(987654321) is True
    assert is_chat_allowed("987654321") is True

    # Blocked
    assert is_chat_allowed("@stranger") is False
    assert is_chat_allowed("12345") is False


@pytest.mark.asyncio
async def test_middleware_blocks_unauthorized_chat(tmp_path):
    cfg_file = tmp_path / "safe_config.json"
    cfg = SafeSecurityConfig(
        safe_mode=True,
        read_only=False,
        allowed_chats=["me"],
    )
    update_security_config(cfg, cfg_file)

    middleware = SafeSecurityMiddleware()
    call_next = AsyncMock(return_value="OK")

    # Mock Context with an unauthorized chat
    ctx = MagicMock()
    ctx.method = "tools/call"
    ctx.params = {
        "name": "send_message",
        "arguments": {"chat_id": "@hacker", "message": "hello"}
    }

    result = await middleware(ctx, call_next)
    assert isinstance(result, CallToolResult)
    assert result.is_error is True
    assert isinstance(result.content[0], TextContent)
    assert "[SECURITY GUARD BLOCKED]" in result.content[0].text
    call_next.assert_not_called()


@pytest.mark.asyncio
async def test_middleware_allows_whitelisted_chat(tmp_path):
    cfg_file = tmp_path / "safe_config.json"
    cfg = SafeSecurityConfig(
        safe_mode=True,
        read_only=False,
        allowed_chats=["me"],
        rate_limit_delay=0.0,
    )
    update_security_config(cfg, cfg_file)

    middleware = SafeSecurityMiddleware()
    call_next = AsyncMock(return_value="SUCCESS")

    ctx = MagicMock()
    ctx.method = "tools/call"
    ctx.params = {
        "name": "send_message",
        "arguments": {"chat_id": "me", "message": "note"}
    }

    result = await middleware(ctx, call_next)
    assert result == "SUCCESS"
    call_next.assert_called_once()


@pytest.mark.asyncio
async def test_middleware_enforces_read_only(tmp_path):
    cfg_file = tmp_path / "safe_config.json"
    cfg = SafeSecurityConfig(
        safe_mode=True,
        read_only=True,
        allowed_chats=["me"],
        rate_limit_delay=0.0,
    )
    update_security_config(cfg, cfg_file)

    middleware = SafeSecurityMiddleware()
    call_next = AsyncMock(return_value="OK")

    ctx = MagicMock()
    ctx.method = "tools/call"
    ctx.params = {
        "name": "send_message",
        "arguments": {"chat_id": "me", "message": "note"}
    }

    result = await middleware(ctx, call_next)
    assert isinstance(result, CallToolResult)
    assert result.is_error is True
    assert isinstance(result.content[0], TextContent)
    assert "READ_ONLY" in result.content[0].text
    call_next.assert_not_called()
