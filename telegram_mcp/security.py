"""Security and hardening layer for Safe-Telegram-MCP.

Provides:
- Chat Whitelist enforcement (prevent data leakage & prompt injection)
- FloodWait / Ban prevention via intelligent Rate-Limiting
- Read-Only Mode enforcement
- Peer Flood protection
- Centralised JSON configuration with live reload
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Set, Union

logger = logging.getLogger("telegram_mcp.security")

DEFAULT_CONFIG_FILE = "safe_config.json"


@dataclass
class SafeSecurityConfig:
    safe_mode: bool = True
    read_only: bool = False
    rate_limit_delay: float = 1.5
    max_messages_limit: int = 50
    allowed_chats: List[str] = field(default_factory=lambda: ["me"])
    prevent_peer_flood: bool = True
    strict_whitelist: bool = False
    accounts: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Union[str, Path] = DEFAULT_CONFIG_FILE) -> "SafeSecurityConfig":
        file_path = Path(path)
        if not file_path.is_file():
            cfg = cls()
            # Check environment variable overrides
            if os.getenv("SAFE_MODE") is not None:
                cfg.safe_mode = os.getenv("SAFE_MODE", "true").lower() in ("true", "1", "yes")
            if os.getenv("READ_ONLY") is not None:
                cfg.read_only = os.getenv("READ_ONLY", "false").lower() in ("true", "1", "yes")
            if os.getenv("RATE_LIMIT_DELAY") is not None:
                try:
                    cfg.rate_limit_delay = float(os.getenv("RATE_LIMIT_DELAY", "1.5"))
                except ValueError:
                    pass
            if os.getenv("ALLOWED_CHATS"):
                cfg.allowed_chats = [
                    c.strip() for c in os.getenv("ALLOWED_CHATS", "").split(",") if c.strip()
                ]
            cfg.save(file_path)
            return cfg

        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            return cls(
                safe_mode=bool(data.get("safe_mode", True)),
                read_only=bool(data.get("read_only", False)),
                rate_limit_delay=float(data.get("rate_limit_delay", 1.5)),
                max_messages_limit=int(data.get("max_messages_limit", 50)),
                allowed_chats=list(data.get("allowed_chats", ["me"])),
                prevent_peer_flood=bool(data.get("prevent_peer_flood", True)),
                strict_whitelist=bool(data.get("strict_whitelist", False)),
                accounts=dict(data.get("accounts", {})),
            )
        except Exception as exc:
            logger.warning("Failed to parse security config %s (%s). Using defaults.", file_path, exc)
            return cls()

    def save(self, path: Union[str, Path] = DEFAULT_CONFIG_FILE) -> None:
        file_path = Path(path)
        try:
            file_path.write_text(
                json.dumps(asdict(self), indent=2, ensure_ascii=False),
                encoding="utf-8"
            )
        except Exception as exc:
            logger.error("Failed to write security config %s: %s", file_path, exc)


_ACTIVE_CONFIG: Optional[SafeSecurityConfig] = None
_LAST_REQUEST_TIME: float = 0.0
_RATE_LOCK = asyncio.Lock()


def get_security_config(account: Optional[str] = None) -> SafeSecurityConfig:
    global _ACTIVE_CONFIG
    if _ACTIVE_CONFIG is None:
        _ACTIVE_CONFIG = SafeSecurityConfig.load()
    if not account or str(account).lower() in ("global", "all", "none"):
        return _ACTIVE_CONFIG

    acc_key = str(account).lower().strip()
    if acc_key in _ACTIVE_CONFIG.accounts:
        acc_data = _ACTIVE_CONFIG.accounts[acc_key]
        return SafeSecurityConfig(
            safe_mode=bool(acc_data.get("safe_mode", _ACTIVE_CONFIG.safe_mode)),
            read_only=bool(acc_data.get("read_only", _ACTIVE_CONFIG.read_only)),
            rate_limit_delay=float(acc_data.get("rate_limit_delay", _ACTIVE_CONFIG.rate_limit_delay)),
            max_messages_limit=int(acc_data.get("max_messages_limit", _ACTIVE_CONFIG.max_messages_limit)),
            allowed_chats=list(acc_data.get("allowed_chats", _ACTIVE_CONFIG.allowed_chats)),
            prevent_peer_flood=bool(acc_data.get("prevent_peer_flood", _ACTIVE_CONFIG.prevent_peer_flood)),
            strict_whitelist=bool(acc_data.get("strict_whitelist", _ACTIVE_CONFIG.strict_whitelist)),
            accounts=_ACTIVE_CONFIG.accounts,
        )
    return _ACTIVE_CONFIG


def update_security_config(new_config: SafeSecurityConfig, path: Union[str, Path] = DEFAULT_CONFIG_FILE) -> None:
    global _ACTIVE_CONFIG
    _ACTIVE_CONFIG = new_config
    _ACTIVE_CONFIG.save(path)


async def enforce_rate_limit() -> None:
    """Enforce minimum delay between calls to Telegram to avoid FloodWait."""
    global _LAST_REQUEST_TIME
    config = get_security_config()
    if not config.safe_mode or config.rate_limit_delay <= 0:
        return

    async with _RATE_LOCK:
        now = time.monotonic()
        elapsed = now - _LAST_REQUEST_TIME
        if elapsed < config.rate_limit_delay:
            delay = config.rate_limit_delay - elapsed
            await asyncio.sleep(delay)
        _LAST_REQUEST_TIME = time.monotonic()


def is_chat_allowed(target: Any) -> bool:
    """Check whether a target chat/user is allowed by the whitelist."""
    if target is None:
        return True

    target_str = str(target).strip()
    if not target_str:
        return True

    config = get_security_config()
    if not config.safe_mode:
        return True

    # If whitelist is completely empty and not strict, allow
    if not config.allowed_chats and not config.strict_whitelist:
        return True

    normalized_target = target_str.lower()
    clean_target = normalized_target.lstrip("@")

    for allowed in config.allowed_chats:
        allowed_str = str(allowed).strip().lower()
        if allowed_str == "me" and normalized_target in ("me", "self"):
            return True
        if allowed_str == normalized_target:
            return True
        if allowed_str.lstrip("@") == clean_target:
            return True

    return False


# Tool classification for Read-Only mode
READ_ONLY_EXEMPT_TOOLS = frozenset([
    "list_accounts",
    "get_me",
    "get_bot_info",
    "get_chats",
    "list_chats",
    "get_chat",
    "get_messages",
    "get_history",
    "search_posts",
    "list_topics",
    "list_folders",
    "get_folder",
    "list_photos",
    "open_photo",
    "list_contact_aliases",
    "get_drafts",
    "get_admins",
    "get_stories",
    "incoming_feed_status",
    "translate",
])


class SafeSecurityMiddleware:
    """Intercepts and validates every MCP tool call for security boundaries."""

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        from mcp.types import CallToolResult, TextContent

        if getattr(ctx, "method", None) == "tools/call":
            params = getattr(ctx, "params", {}) or {}
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {}) or {}

            account = arguments.get("account")
            config = get_security_config(account=account)

            # 1. Read-Only Mode Check
            if config.safe_mode and config.read_only:
                if tool_name not in READ_ONLY_EXEMPT_TOOLS and not tool_name.startswith(("get_", "list_", "search_")):
                    return CallToolResult(
                        content=[
                            TextContent(
                                type="text",
                                text=(
                                    f"[SECURITY GUARD BLOCKED] Action '{tool_name}' rejected: "
                                    f"Account '{account or 'default'}' is operating in READ_ONLY mode. Modify settings in safe_config.json "
                                    "or via the Safe-Telegram-MCP Web Dashboard to permit write actions."
                                ),
                            )
                        ],
                        is_error=True,
                    )

            # 2. Whitelist Check on chat/user targets
            chat_target = (
                arguments.get("chat_id")
                or arguments.get("peer")
                or arguments.get("user_id")
                or arguments.get("target_chat")
                or arguments.get("channel")
            )
            if chat_target is not None and not is_chat_allowed(chat_target, account=account):
                return CallToolResult(
                    content=[
                        TextContent(
                            type="text",
                            text=(
                                f"[SECURITY GUARD BLOCKED] Target chat '{chat_target}' is not in your allowed whitelist.\n"
                                f"Configured whitelist: {config.allowed_chats}.\n"
                                "To interact with this chat, whitelist it via the Safe-Telegram-MCP Web Dashboard "
                                "or add it to 'allowed_chats' in safe_config.json."
                            ),
                        )
                    ],
                    is_error=True,
                )

            # 3. Parameter Safety Clamping
            if "limit" in arguments and isinstance(arguments["limit"], (int, float)):
                if arguments["limit"] > config.max_messages_limit:
                    arguments["limit"] = config.max_messages_limit

            # 4. Anti-Flood Delay
            await enforce_rate_limit()

        return await call_next(ctx)


def install_security_guard(server: Any) -> None:
    """Install the Safe Security middleware at the head of the MCP server."""
    if any(isinstance(m, SafeSecurityMiddleware) for m in server.middleware):
        return
    server.middleware.insert(0, SafeSecurityMiddleware())
    logger.info("Safe-Telegram-MCP security guard installed successfully.")
