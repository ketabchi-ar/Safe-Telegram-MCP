"""Safe-Telegram-MCP Web Dashboard.

Provides a user-friendly management interface to:
- Configure Telegram API Credentials (API ID, API Hash)
- Toggle Safe Mode and Read-Only Mode
- Add/Remove whitelisted chats with ease
- Export and Import whitelisted chats (JSON)
- Interactive in-browser QR Code Telegram login
- Automatic free port detection
- Bilingual support (Persian with Vazirmatn font / English)
- Adjust anti-flood rate limits with MTProto/Telethon documented guidelines
- Check Telegram connection and session status
- View account ban recovery and troubleshooting guide
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import socket
import tempfile
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route
import uvicorn
import qrcode
from dotenv import load_dotenv

from telegram_mcp.security import (
    DEFAULT_CONFIG_FILE,
    SafeSecurityConfig,
    get_security_config,
    update_security_config,
)
from telegram_mcp.client_identity import client_identity_kwargs

# Load environment safely without triggering settings.py validation
load_dotenv()

# Active QR login task state
_QR_STATE = {
    "client": None,
    "qr": None,
    "task": None,
    "status": "idle",
    "error_message": "",
    "qr_data_uri": "",
    "expires_at": "",
    "label": "default",
    "env_key": "TELEGRAM_SESSION_STRING",
}


def _safe_write_env_key(key: str, value: str, env_path: Path = Path(".env")) -> None:
    """Safely write or update an environment variable in .env without triggering validation."""
    lines = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)

    replaced = False
    for index, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[index] = f"{key}={value}\n"
            replaced = True
            break
    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{key}={value}\n")

    fd, tmp = tempfile.mkstemp(dir=str(env_path.parent.resolve()), prefix=env_path.name + ".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write("".join(lines))
        handle.flush()
        os.fsync(handle.fileno())

    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, env_path)
    os.environ[key] = value

def _safe_remove_env_key(key: str, env_path: Path = Path(".env")) -> None:
    """Safely remove an environment variable from .env."""
    if not env_path.exists():
        return
    lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
    new_lines = [line for line in lines if not line.startswith(f"{key}=")]
    fd, tmp = tempfile.mkstemp(dir=str(env_path.parent.resolve()), prefix=env_path.name + ".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write("".join(new_lines))
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, env_path)
    os.environ.pop(key, None)


_ACCOUNT_INFO_CACHE: dict[str, dict[str, Any]] = {}

def _list_env_accounts(env_path: Path = Path(".env")) -> list[dict[str, Any]]:
    """List all accounts configured in .env with raw values for verification."""
    accounts = []
    if not env_path.exists():
        return accounts
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip("'\"")
        if not v:
            continue
        if k == "TELEGRAM_SESSION_STRING":
            accounts.append({
                "label": "default", 
                "key": k, 
                "preview": (v[:8] + "..." + v[-4:]) if len(v) > 15 else "configured",
                "raw_value": v,
                "type": "string"
            })
        elif k.startswith("TELEGRAM_SESSION_STRING_"):
            label = k[len("TELEGRAM_SESSION_STRING_"):].lower()
            accounts.append({
                "label": label, 
                "key": k, 
                "preview": (v[:8] + "..." + v[-4:]) if len(v) > 15 else "configured",
                "raw_value": v,
                "type": "string"
            })
        elif k == "TELEGRAM_SESSION_NAME":
            accounts.append({
                "label": "default", 
                "key": k, 
                "preview": v,
                "raw_value": v,
                "type": "file"
            })
        elif k.startswith("TELEGRAM_SESSION_NAME_"):
            label = k[len("TELEGRAM_SESSION_NAME_"):].lower()
            accounts.append({
                "label": label, 
                "key": k, 
                "preview": v,
                "raw_value": v,
                "type": "file"
            })
    return accounts


async def _verify_account_live(key: str, value: str) -> dict[str, Any]:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telegram_mcp.client_identity import client_identity_kwargs

    load_dotenv(override=True)
    api_id_val = os.getenv("TELEGRAM_API_ID", "2040")
    api_hash = os.getenv("TELEGRAM_API_HASH", "b18441a1ff607e10a989891a5462e627")
    try:
        api_id = int(api_id_val)
    except ValueError:
        api_id = 2040

    is_string = key.startswith("TELEGRAM_SESSION_STRING")
    session = StringSession(value) if is_string else value

    kwargs = client_identity_kwargs()
    if api_id == 2040:
        kwargs.setdefault("device_model", "Telegram Desktop")
        kwargs.setdefault("system_version", "macOS 15.3")
        kwargs.setdefault("app_version", "5.10.3")

    try:
        from telegram_mcp.proxy import _build_proxy_for_label
        proxy, connection = _build_proxy_for_label("")
        if proxy is not None:
            kwargs["proxy"] = proxy
        if connection is not None:
            kwargs["connection"] = connection
    except Exception:
        pass

    res = {
        "status": "unknown",
        "name": "",
        "username": "",
        "user_id": None,
        "phone": "",
        "error": ""
    }

    client = TelegramClient(session, api_id, api_hash, **kwargs)
    try:
        await asyncio.wait_for(client.connect(), timeout=8.0)
        auth = await client.is_user_authorized()
        if not auth:
            res["status"] = "revoked"
            res["error"] = "نشست در تلگرام باطل شده (Logged out from device)"
        else:
            me = await client.get_me()
            res["status"] = "active"
            first = getattr(me, "first_name", "") or ""
            last = getattr(me, "last_name", "") or ""
            res["name"] = f"{first} {last}".strip()
            res["username"] = getattr(me, "username", "") or ""
            res["user_id"] = getattr(me, "id", None)
            phone = getattr(me, "phone", "") or ""
            if phone:
                res["phone"] = "+" + phone[:4] + "****" + phone[-4:] if len(phone) > 7 else "+" + phone
    except Exception as exc:
        err_name = type(exc).__name__
        if "AuthKeyUnregistered" in err_name or "SessionRevoked" in err_name:
            res["status"] = "revoked"
            res["error"] = "نشست توسط کاربر باطل شده است"
        else:
            res["status"] = "error"
            res["error"] = str(exc)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    return res


async def _verify_all_accounts(force: bool = False) -> list[dict[str, Any]]:
    accounts = _list_env_accounts()
    tasks = []
    keys_to_check = []
    now = time.time()
    for acc in accounts:
        key = acc["key"]
        val = acc.get("raw_value") or os.getenv(key, "")
        cached = _ACCOUNT_INFO_CACHE.get(key)
        if not force and cached and (now - cached.get("checked_at", 0)) < 90:
            acc.update(cached.get("data", {}))
        else:
            keys_to_check.append(acc)
            tasks.append(_verify_account_live(key, val))

    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for acc, res in zip(keys_to_check, results):
            if isinstance(res, Exception):
                data = {"status": "error", "error": str(res)}
            else:
                data = res
            acc.update(data)
            _ACCOUNT_INFO_CACHE[acc["key"]] = {
                "data": data,
                "checked_at": now
            }

    return accounts


def _generate_mcp_configs(repo_dir: str) -> dict[str, str]:
    return {
        "hermes": f"hermes mcp add telegram --command uv --args --directory {repo_dir} run main.py",
        "claude": json.dumps({
            "mcpServers": {
                "telegram": {
                    "command": "uv",
                    "args": ["--directory", repo_dir, "run", "main.py"]
                }
            }
        }, indent=2),
        "cursor": json.dumps({
            "mcpServers": {
                "telegram": {
                    "command": "uv",
                    "args": ["--directory", repo_dir, "run", "main.py"]
                }
            }
        }, indent=2),
        "codex": f"codex mcp add telegram -- uv --directory {repo_dir} run main.py",
    }


def _generate_qr_data_uri(url: str) -> str:
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def find_available_port(start_port: int = 8080, max_tries: int = 50) -> int:
    """Find the first open port starting from start_port."""
    for port in range(start_port, start_port + max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start_port


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="fa" dir="rtl" class="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Safe-Telegram-MCP Dashboard</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet">
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        extend: {
          colors: {
            brand: {
              500: '#38bdf8',
              600: '#0284c7',
            }
          }
        }
      }
    }
  </script>
  <style>
    body { background-color: #0b0f19; color: #f8fafc; font-family: 'Vazirmatn', system-ui, -apple-system, sans-serif; }
    html[dir="ltr"] body { font-family: system-ui, -apple-system, sans-serif; }
  </style>
</head>
<body class="min-h-screen bg-[#0b0f19] text-slate-100 flex flex-col items-center p-4 md:p-8 transition-all">
  <div class="max-w-4xl w-full space-y-6">
    
    <!-- Header -->
    <header class="flex flex-col md:flex-row justify-between items-start md:items-center bg-slate-900/90 border border-slate-800 rounded-2xl p-6 shadow-2xl gap-4 backdrop-blur">
      <div>
        <div class="flex items-center gap-3">
          <div class="w-11 h-11 rounded-xl bg-sky-500/10 text-sky-400 flex items-center justify-center font-bold text-2xl border border-sky-500/30 shadow-inner">
            🛡️
          </div>
          <div>
            <h1 class="text-2xl font-black tracking-tight text-white flex items-center gap-2">
              <span>Safe-Telegram-MCP</span>
              <span class="text-xs px-2 py-0.5 rounded-full bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 font-medium" id="badgeHardened">امن‌سازی شده</span>
            </h1>
            <p class="text-xs text-slate-400" id="headerSubtitle">لیست سفید چت‌ها، جلوگیری از نشت داده و ضدبن هوشمند تلگرام</p>
          </div>
        </div>
      </div>
      <div class="flex flex-wrap items-center gap-2.5">
        <button onclick="toggleLang()" class="flex items-center gap-1.5 px-3 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 text-xs font-semibold transition-colors">
          🌐 <span id="langBtnText">English</span>
        </button>
        <button onclick="openCredentialsModal()" class="flex items-center gap-1.5 px-3 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 text-xs font-semibold transition-colors">
          <span>🔑</span> <span id="btnApiKeys">کلیدهای API</span>
        </button>
        <button onclick="openQrModal()" class="flex items-center gap-1.5 px-3 py-2 rounded-xl bg-sky-500/10 hover:bg-sky-500/20 text-sky-400 border border-sky-500/30 text-xs font-semibold transition-colors">
          <span>📲</span> <span id="btnQrLogin">ورود QR</span>
        </button>
        <div id="statusBadge" class="flex items-center gap-2 px-3 py-2 rounded-xl bg-slate-950 border border-slate-800 text-xs font-medium">
          <span class="w-2.5 h-2.5 rounded-full bg-slate-500 animate-pulse" id="statusDot"></span>
          <span id="statusText">بررسی وضعیت...</span>
        </div>
      </div>
    </header>

    <!-- Accounts Management Section -->
    <section class="bg-slate-900/90 border border-slate-800 rounded-2xl p-6 shadow-xl space-y-4">
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
        <h2 class="text-lg font-bold text-white flex items-center gap-2">
          <span>👥</span> <span id="titleAccounts">حساب‌های تلگرام (Telegram Accounts)</span>
        </h2>
        <div class="flex items-center gap-2">
          <span id="accountsCountBadge" class="text-xs font-mono px-2 py-0.5 rounded bg-slate-800 text-sky-400 border border-slate-700">0 حساب</span>
          <button onclick="checkAccountsLive()" id="btnCheckAccounts" class="text-xs px-2.5 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 font-semibold rounded-xl border border-slate-700 transition-colors flex items-center gap-1">
            <span>🔄</span> <span id="btnCheckAccountsText">بررسی اتصال</span>
          </button>
          <button onclick="openQrModal('new')" class="text-xs px-3 py-1.5 bg-sky-500 hover:bg-sky-400 text-slate-950 font-bold rounded-xl transition-colors shadow-lg shadow-sky-500/10" id="btnNewAccount">
            + افزودن حساب جدید
          </button>
        </div>
      </div>
      <p class="text-xs text-slate-400 leading-relaxed" id="descAccounts">
        امکان اتصال همزمان چندین اکانت وجود دارد. هوش مصنوعی با پارامتر <code class="text-sky-400 font-mono">account="برچسب"</code> بین اکانت‌ها سوییچ می‌کند.
      </p>
      <div id="accountsContainer" class="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div class="text-xs text-slate-500 py-3 text-center border border-dashed border-slate-800 rounded-xl md:col-span-2" id="loadingAccounts">در حال بارگذاری حساب‌ها...</div>
      </div>
    </section>

    <!-- Active Account Context Switcher -->
    <div class="bg-slate-900/90 border border-slate-800 rounded-2xl p-4 shadow-xl flex flex-col md:flex-row items-start md:items-center justify-between gap-3">
      <div class="flex items-center gap-2.5">
        <div class="w-8 h-8 rounded-lg bg-sky-500/10 text-sky-400 flex items-center justify-center font-bold text-sm border border-sky-500/20">
          🎯
        </div>
        <div>
          <div class="text-xs text-slate-400">تنظیمات لیست سفید و سیاست‌های امنیتی برای:</div>
          <div class="text-sm font-bold text-white flex items-center gap-2 mt-0.5" id="activeAccountTitle">
            <span class="text-sky-400 font-mono" id="activeAccountLabelBadge">حساب پیش‌فرض (default)</span>
          </div>
        </div>
      </div>
      <div class="flex items-center gap-1.5 flex-wrap" id="accountTabsContainer">
        <!-- Tabs injected dynamically via renderAccountTabs() -->
      </div>
    </div>

    <!-- Main Grid -->
    <div class="grid grid-cols-1 md:grid-cols-2 gap-6" id="settingsGrid">
      
      <!-- Whitelist Management -->
      <section class="bg-slate-900/90 border border-slate-800 rounded-2xl p-6 shadow-xl flex flex-col justify-between">
        <div>
          <div class="flex items-center justify-between mb-3">
            <h2 class="text-lg font-bold text-white flex items-center gap-2">
              <span>📋</span> <span id="titleWhitelist">چت‌های مجاز (Whitelist)</span>
            </h2>
            <div class="flex items-center gap-2">
              <button onclick="exportWhitelist()" class="text-xs px-2.5 py-1 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg border border-slate-700 transition-colors">
                ⬇️ <span id="btnExport">خروجی</span>
              </button>
              <label class="text-xs px-2.5 py-1 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg border border-slate-700 cursor-pointer transition-colors">
                ⬆️ <span id="btnImport">ورودی</span>
                <input type="file" id="importFileInput" class="hidden" accept=".json" onchange="importWhitelist(event)">
              </label>
              <span id="whitelistCount" class="text-xs font-mono px-2 py-0.5 rounded bg-slate-800 text-sky-400 border border-slate-700">0 چت</span>
            </div>
          </div>
          <p class="text-xs text-slate-400 mb-4 leading-relaxed" id="descWhitelist">
            هوش مصنوعی <strong class="text-slate-200">فقط</strong> به چت‌های این لیست دسترسی دارد. کلیه پیام‌های شخصی، خانوادگی، بانکی و سایر گروه‌ها کاملاً مسدود و غیرقابل خواندن هستند.
          </p>

          <!-- Add chat form -->
          <div class="flex gap-2 mb-4">
            <input 
              id="newChatInput" 
              type="text" 
              placeholder="مثال: me یا mychannel@ یا 12345678" 
              class="flex-1 bg-slate-950 border border-slate-800 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:border-sky-500 text-white placeholder-slate-500 transition-colors"
              onkeydown="if(event.key==='Enter') addChat()"
            />
            <button 
              onclick="addChat()" 
              class="bg-sky-500 hover:bg-sky-400 text-slate-950 font-bold px-4 py-2.5 rounded-xl text-sm transition-colors shadow-lg shadow-sky-500/10" id="btnAdd">
              افزودن
            </button>
          </div>

          <!-- List -->
          <ul id="whitelistContainer" class="space-y-2 max-h-64 overflow-y-auto pr-1">
            <li class="text-xs text-slate-500 py-3 text-center" id="loadingChats">در حال بارگذاری چت‌های مجاز...</li>
          </ul>
        </div>
        
        <div class="mt-4 pt-3 border-t border-slate-800/80 text-[11px] text-slate-500" id="tipSavedMessages">
          💡 نکته: با افزودن <code class="text-sky-400 font-mono">me</code> هوش مصنوعی فقط به <em>پیام‌های ذخیره‌شده (Saved Messages)</em> دسترسی خواهد داشت.
        </div>
      </section>

      <!-- Security & Anti-Flood Controls -->
      <section class="bg-slate-900/90 border border-slate-800 rounded-2xl p-6 shadow-xl space-y-5">
        <h2 class="text-lg font-bold text-white flex items-center gap-2">
          <span>⚙️</span> <span id="titlePolicies">سیاست‌های امنیتی و ضدبن</span>
        </h2>

        <!-- Safe Mode Toggle -->
        <div class="flex items-center justify-between p-3 rounded-xl bg-slate-950 border border-slate-800/80">
          <div>
            <div class="font-semibold text-sm text-slate-200" id="optSafeMode">حالت امن (Safe Mode)</div>
            <div class="text-xs text-slate-400" id="descSafeMode">اجبار محدودیت‌های لیست سفید و تاخیر ضداسپم</div>
          </div>
          <label class="relative inline-flex items-center cursor-pointer">
            <input id="toggleSafeMode" type="checkbox" class="sr-only peer" onchange="saveConfig()">
            <div class="w-11 h-6 bg-slate-700 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-sky-500"></div>
          </label>
        </div>

        <!-- Read Only Toggle -->
        <div class="flex items-center justify-between p-3 rounded-xl bg-slate-950 border border-slate-800/80">
          <div>
            <div class="font-semibold text-sm text-slate-200" id="optReadOnly">حالت فقط خواندنی (Read-Only)</div>
            <div class="text-xs text-slate-400" id="descReadOnly">مسدودسازی کامل ارسال، ویرایش یا حذف پیام‌ها</div>
          </div>
          <label class="relative inline-flex items-center cursor-pointer">
            <input id="toggleReadOnly" type="checkbox" class="sr-only peer" onchange="saveConfig()">
            <div class="w-11 h-6 bg-slate-700 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-rose-500"></div>
          </label>
        </div>

        <!-- Anti-Peer Flood -->
        <div class="flex items-center justify-between p-3 rounded-xl bg-slate-950 border border-slate-800/80">
          <div>
            <div class="font-semibold text-sm text-slate-200" id="optPeerFlood">مهار پیام به غریبه‌ها (Anti-Peer Flood)</div>
            <div class="text-xs text-slate-400" id="descPeerFlood">جلوگیری از ارسال پیام به شماره‌های خارج از مخاطبین</div>
          </div>
          <label class="relative inline-flex items-center cursor-pointer">
            <input id="togglePeerFlood" type="checkbox" class="sr-only peer" onchange="saveConfig()">
            <div class="w-11 h-6 bg-slate-700 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-emerald-500"></div>
          </label>
        </div>

        <!-- Rate Limit Slider -->
        <div class="space-y-2 p-3.5 rounded-xl bg-slate-950 border border-slate-800/80">
          <div class="flex justify-between items-center">
            <span class="text-sm font-semibold text-slate-200" id="optDelay">وقفه بین درخواست‌ها (Delay)</span>
            <span id="rateLimitLabel" class="text-xs font-mono font-bold text-sky-400 bg-sky-500/10 px-2 py-0.5 rounded border border-sky-500/20">1.5s</span>
          </div>
          <input 
            id="rateLimitInput" 
            type="range" 
            min="0.5" 
            max="5.0" 
            step="0.5" 
            value="1.5" 
            class="w-full h-2 bg-slate-700 rounded-lg appearance-none cursor-pointer accent-sky-500"
            oninput="document.getElementById('rateLimitLabel').textContent = this.value + 's'"
            onchange="saveConfig()"
          />
          <div class="text-[11px] text-slate-400 flex justify-between pt-1">
            <span id="labelAggressive">سریع (0.5s)</span>
            <span class="text-sky-400 font-semibold" id="labelRecommended">پیشنهادی (1.5s - 2.0s)</span>
            <span id="labelUltraSafe">فوق امن (5.0s)</span>
          </div>
        </div>

      </section>
    </div>

    <!-- Documentation & Guidelines Card -->
    <section class="bg-slate-900/90 border border-slate-800 rounded-2xl p-6 shadow-xl space-y-3">
      <div class="flex items-center gap-2">
        <span class="text-sky-400 text-lg">📊</span>
        <h3 class="text-base font-bold text-white" id="titleDocWhy">چرا این اعداد لیمیت؟ (مستندات رسمی MTProto و Telethon)</h3>
      </div>
      <p class="text-xs text-slate-300 leading-relaxed" id="descDocWhy">
        سیستم هوشمند ضداسپم تلگرام فرکانس درخواست‌های ارسالی کلاینت را رصد می‌کند:
      </p>
      <div class="grid grid-cols-1 md:grid-cols-3 gap-3 text-xs pt-1">
        <div class="p-3 rounded-xl bg-slate-950 border border-slate-800">
          <div class="font-bold text-sky-400 mb-1" id="box1Title">⏱️ وقفه ۱.۵ الی ۲.۰ ثانیه</div>
          <div class="text-slate-400" id="box1Text">تلگرام ارسال بیش از ۲۵-۳۰ درخواست در دقیقه را محدود و خطای FloodWait صادر می‌کند. وقفه ۱.۵s اجرای امن زیر ۴۰ درخواست در دقیقه را تضمین می‌کند.</div>
        </div>
        <div class="p-3 rounded-xl bg-slate-950 border border-slate-800">
          <div class="font-bold text-emerald-400 mb-1" id="box2Title">📦 سقف ۵۰ پیام در هر نوبت</div>
          <div class="text-slate-400" id="box2Text">درخواست بیش از ۵۰ تا ۱۰۰ پیام در هر کوئری موجب فعال شدن فیلتر بار سنگین سرور می‌شود. سرور این ابزار سقف دریافت را روی ۵۰ قفل کرده است.</div>
        </div>
        <div class="p-3 rounded-xl bg-slate-950 border border-slate-800">
          <div class="font-bold text-amber-400 mb-1" id="box3Title">🚫 عدم پیام به افراد ناشناس</div>
          <div class="text-slate-400" id="box3Text">شروع مکالمه با بیش از ۵ الی ۱۰ فرد خارج از مخاطبین در روز باعث فعال شدن خودکار SpamBlocker تلگرام می‌شود. گارد ابزار این قابلیت را مهار می‌کند.</div>
        </div>
      </div>
    </section>

    <!-- Ban Recovery Playbook -->
    <section class="bg-slate-900/90 border border-slate-800 rounded-2xl p-6 shadow-xl space-y-4">
      <div class="flex items-center justify-between cursor-pointer" onclick="togglePlaybook()">
        <div class="flex items-center gap-2">
          <span class="text-rose-400 text-lg">🚨</span>
          <h3 class="text-base font-bold text-white" id="titlePlaybook">راهنمای مواقع محدودیت یا بن شدن تلگرام (Recovery Playbook)</h3>
        </div>
        <span id="playbookChevron" class="text-slate-400 text-sm transform transition-transform">▼</span>
      </div>
      
      <div id="playbookBody" class="space-y-4 text-xs text-slate-300 border-t border-slate-800 pt-4">
        
        <div>
          <h4 class="font-bold text-amber-400 text-sm mb-1" id="pb1Title">۱. خطای موقت FloodWait (مانند FloodWaitError: A wait of X seconds is required)</h4>
          <p class="text-slate-400 leading-relaxed" id="pb1Text">
            • <strong>ماهیت:</strong> جریمه زمانی موقت تلگرام به دلیل ارسال رگباری درخواست‌ها.<br>
            • <strong>راهکار حیاتی:</strong> <span class="text-rose-300 font-semibold">به هیچ وجه تلاش مجدد نکنید!</span> هر تلاش مجدد در زمان وقفه، ثانیه‌شمار تلگرام را دو برابر یا ریست می‌کند. فقط تا پایان ثانیه‌ها صبر کنید. سیستم ابزار خودکار این زمان را مدیریت می‌کند.
          </p>
        </div>

        <div>
          <h4 class="font-bold text-amber-400 text-sm mb-1" id="pb2Title">۲. میوت یا ریپورت اسپم (PeerFloodError: عدم امکان ارسال پیام به غریبه‌ها)</h4>
          <p class="text-slate-400 leading-relaxed" id="pb2Text">
            • <strong>ماهیت:</strong> اکانت موقتاً از ارسال پیام به افراد ناشناس منع شده است.<br>
            • <strong>راهکار:</strong> وارد ربات رسمی <a href="https://t.me/SpamBot" target="_blank" class="text-sky-400 underline font-mono">SpamBot@</a> شوید، دستور <em>Start/</em> را بزنید و گزینه‌های <em>"This is a mistake"</em> و <em>"I never send unsolicited messages"</em> را بزنید. محدودیت اول معمولاً ظرف ۲۴ الی ۴۸ ساعت خودکار رفع می‌شود.
          </p>
        </div>

        <div>
          <h4 class="font-bold text-rose-400 text-sm mb-1" id="pb3Title">۳. مسدودی کامل شماره (PHONE_NUMBER_BANNED)</h4>
          <p class="text-slate-400 leading-relaxed" id="pb3Text">
            • <strong>ایمیل رسمی رفع مسدودی:</strong> ارسال ایمیل به <code class="text-sky-400 bg-slate-950 px-1 py-0.5 rounded">recover@telegram.org</code> و <code class="text-sky-400 bg-slate-950 px-1 py-0.5 rounded">login@telegram.org</code>.<br>
            • <strong>موضوع ایمیل:</strong> <code class="text-slate-200 bg-slate-950 px-1.5 py-0.5 rounded">Banned phone number: +98XXXXXXXXXX</code><br>
            • <strong>متن استاندارد:</strong> <em>"Hello Telegram Support Team. My phone number was banned unexpectedly. I was using a local MTProto development client on my personal workstation. I never engaged in spam or violations of Terms of Service. Please review and restore my account."</em><br>
            • <strong>پیگیری تکمیلی:</strong> ارسال تیکت در <a href="https://telegram.org/support" target="_blank" class="text-sky-400 underline font-mono">telegram.org/support</a> و پیام به اکانت‌های رسمی <a href="https://twitter.com/smstelegram" target="_blank" class="text-sky-400 underline">smstelegram@</a> یا <a href="https://twitter.com/telegram" target="_blank" class="text-sky-400 underline">Telegram@</a> در توییتر/ایکس.
          </p>
        </div>

      </div>
    </section>

    <!-- MCP Integration Guide -->
    <section class="bg-slate-900/90 border border-slate-800 rounded-2xl p-6 shadow-xl space-y-4">
      <div class="flex items-center justify-between">
        <h2 class="text-lg font-bold text-white flex items-center gap-2">
          <span>⚡</span> <span id="titleMcpGuide">راهنمای اتصال به کلاینت‌های MCP</span>
        </h2>
        <span class="text-xs font-mono px-2.5 py-1 rounded-lg bg-sky-500/10 text-sky-400 border border-sky-500/20">Hermes • Claude • Cursor • Codex</span>
      </div>
      <p class="text-xs text-slate-400 leading-relaxed" id="descMcpGuide">
        پس از لاگین، دستور متناسب با ابزار AI خود را با یک کلیک کپی کرده و اجرا کنید:
      </p>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-4" id="mcpCardsContainer">
        <!-- Injected via JS -->
      </div>
    </section>

    <!-- Footer -->
    <footer class="bg-slate-900/60 border border-slate-800/80 rounded-xl p-4 text-xs text-slate-400 flex flex-col md:flex-row justify-between items-center gap-2">
      <div id="footerSaved">تنظیمات خودکار در فایل <code class="text-sky-400 bg-slate-950 px-1.5 py-0.5 rounded font-mono">safe_config.json</code> ذخیره می‌شوند.</div>
      <div>Safe-Telegram-MCP • Hardened Edition</div>
    </footer>

  </div>

  <!-- API Keys Modal -->
  <div id="credentialsModal" class="fixed inset-0 bg-slate-950/80 backdrop-blur-sm z-50 flex items-center justify-center p-4 hidden">
    <div class="bg-slate-900 border border-slate-800 rounded-2xl max-w-md w-full p-6 space-y-4 shadow-2xl relative">
      <button onclick="closeCredentialsModal()" class="absolute top-4 left-4 text-slate-400 hover:text-white text-lg">✕</button>
      
      <h3 class="text-lg font-bold text-white flex items-center gap-2">
        <span>🔑</span> <span id="modalApiTitle">کلیدهای API تلگرام</span>
      </h3>
      <p class="text-xs text-slate-400" id="modalApiDesc">
        شناسه و هش برنامه خود را رایگان از <a href="https://my.telegram.org/apps" target="_blank" class="text-sky-400 underline">my.telegram.org/apps</a> دریافت کنید:
      </p>

      <div class="p-3 bg-sky-500/10 border border-sky-500/20 rounded-xl space-y-2">
        <div class="flex items-center justify-between">
          <span class="text-xs font-bold text-sky-300" id="quickSetupTitle">⚡ راهکار فوری بدون دردسر:</span>
          <button type="button" onclick="useOfficialDesktopKeys()" class="text-xs px-2.5 py-1 bg-sky-500 hover:bg-sky-400 text-slate-950 font-bold rounded-lg transition-colors" id="btnFillDesktopKeys">
            استفاده از کلیدهای رسمی دسکتاپ
          </button>
        </div>
        <p class="text-[11px] text-slate-400 leading-relaxed" id="quickSetupDesc">
          اگر در سایت my.telegram.org خطای ERROR دریافت می‌کنید، با زدن این دکمه از کلیدهای متن‌باز تلگرام دسکتاپ (2040) استفاده کنید و مستقیم وارد شوید.
        </p>
      </div>

      <div class="space-y-3 pt-2">
        <div>
          <label class="text-xs font-semibold text-slate-300 block mb-1">TELEGRAM_API_ID</label>
          <input id="apiIdInput" type="text" placeholder="مثال: 1234567" class="w-full bg-slate-950 border border-slate-800 rounded-xl px-4 py-2 text-sm text-white focus:outline-none focus:border-sky-500 font-mono">
        </div>
        <div>
          <label class="text-xs font-semibold text-slate-300 block mb-1">TELEGRAM_API_HASH</label>
          <input id="apiHashInput" type="text" placeholder="مثال: 0123456789abcdef0123456789abcdef" class="w-full bg-slate-950 border border-slate-800 rounded-xl px-4 py-2 text-sm text-white focus:outline-none focus:border-sky-500 font-mono">
        </div>
      </div>

      <div class="flex justify-end gap-2 pt-2">
        <button onclick="closeCredentialsModal()" class="px-4 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs" id="btnCancelApi">
          انصراف
        </button>
        <button onclick="saveCredentials()" class="px-4 py-2 rounded-xl bg-sky-500 hover:bg-sky-400 text-slate-950 text-xs font-bold transition-colors" id="btnSaveApi">
          ذخیره کلیدها
        </button>
      </div>
    </div>
  </div>

  <!-- Edit Account Modal -->
  <div id="editAccountModal" class="fixed inset-0 bg-slate-950/80 backdrop-blur-sm z-50 flex items-center justify-center p-4 hidden">
    <div class="bg-slate-900 border border-slate-800 rounded-2xl max-w-md w-full p-6 space-y-4 shadow-2xl relative text-right">
      <button onclick="closeEditAccountModal()" class="absolute top-4 left-4 text-slate-400 hover:text-white text-lg">✕</button>
      
      <h3 class="text-lg font-bold text-white flex items-center gap-2">
        <span>✏️</span> <span id="modalEditTitle">ویرایش حساب تلگرام</span>
      </h3>
      <p class="text-xs text-slate-400" id="modalEditDesc">
        می‌توانید نام برچسب (Label) این حساب را تغییر دهید تا در ابزارهای هوش مصنوعی با نام دلخواه شما صدا زده شود:
      </p>

      <input type="hidden" id="editOldKey" />

      <div class="space-y-3 pt-2">
        <div>
          <label class="text-xs font-semibold text-slate-300 block mb-1" id="lblEditLabel">نام برچسب حساب (Label):</label>
          <input id="editLabelInput" type="text" placeholder="مثال: default یا work یا personal" class="w-full bg-slate-950 border border-slate-800 rounded-xl px-4 py-2 text-sm text-white focus:outline-none focus:border-sky-500 font-mono" />
          <span class="text-[10px] text-slate-500 mt-1 block">در صورت انتخاب default، متغیر استاندارد TELEGRAM_SESSION_STRING قرار می‌گیرد.</span>
        </div>

        <div>
          <label class="text-xs font-semibold text-slate-300 block mb-1">کلید فعلی در .env:</label>
          <input id="editKeyPreview" type="text" disabled class="w-full bg-slate-950/60 border border-slate-800/80 rounded-xl px-4 py-2 text-xs text-slate-400 font-mono" />
        </div>
      </div>

      <div class="flex justify-end gap-2 pt-2">
        <button onclick="closeEditAccountModal()" class="px-4 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs">انصراف</button>
        <button onclick="submitEditAccount()" class="px-4 py-2 rounded-xl bg-sky-500 hover:bg-sky-400 text-slate-950 font-bold text-xs shadow-lg shadow-sky-500/10">ذخیره تغییرات</button>
      </div>
    </div>
  </div>

  <!-- QR Login Wizard Modal -->
  <div id="qrModal" class="fixed inset-0 bg-slate-950/80 backdrop-blur-sm z-50 flex items-center justify-center p-4 hidden">
    <div class="bg-slate-900 border border-slate-800 rounded-2xl max-w-lg w-full p-6 space-y-4 shadow-2xl relative">
      <button onclick="closeQrModal()" class="absolute top-4 left-4 text-slate-400 hover:text-white text-lg">✕</button>
      
      <!-- Wizard Step 1 & 2: Setup & Scan -->
      <div id="qrWizardStepScan" class="space-y-4">
        <div class="text-center">
          <h3 class="text-lg font-bold text-white flex items-center justify-center gap-2">
            <span>📲</span> <span id="modalQrTitle">اتصال به تلگرام با QR (چنداکانته)</span>
          </h3>
          <p class="text-xs text-slate-400 leading-relaxed mt-1" id="modalQrDesc">
            کد QR را با اپلیکیشن تلگرام در گوشی خود اسکن کنید:
            <br><strong class="text-slate-200">Settings > Devices > Link Desktop Device</strong>
          </p>
        </div>

        <!-- Account Label Input -->
        <div class="bg-slate-950 border border-slate-800 rounded-xl p-3.5 space-y-2 text-right">
          <div class="flex items-center justify-between">
            <label class="text-xs font-semibold text-slate-300" id="lblAccountLabel">🏷️ نام برچسب حساب (Account Label):</label>
            <span class="text-[10px] text-sky-400 font-mono" id="lblKeyPreview">TELEGRAM_SESSION_STRING</span>
          </div>
          <input 
            id="accountLabelInput" 
            type="text" 
            value="default" 
            placeholder="مثال: default یا work یا personal" 
            class="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white focus:outline-none focus:border-sky-500 font-mono"
            oninput="updateLabelKeyPreview(this.value)"
          />
          <p class="text-[11px] text-slate-400 leading-relaxed" id="lblAccountLabelDesc">
            برای حساب اصلی عبارت <code class="text-sky-300 font-mono">default</code> و برای سایر حساب‌ها یک نام انگلیسی دلخواه (مانند <code class="text-sky-300 font-mono">work</code> یا <code class="text-sky-300 font-mono">second</code>) وارد کنید.
          </p>
        </div>

        <div id="qrContainer" class="p-4 bg-white rounded-xl flex items-center justify-center min-h-[220px]">
          <div id="qrSpinner" class="text-xs text-slate-600 animate-pulse">در حال برقراری ارتباط با MTProto...</div>
          <img id="qrImage" class="hidden w-52 h-52 mx-auto" alt="Scan QR" />
        </div>

        <div id="qrStatusText" class="text-xs text-slate-400 text-center font-medium">منتظر اسکن...</div>

        <div class="flex gap-2 justify-center pt-1">
          <button onclick="startQrAuth()" class="px-4 py-2 rounded-xl bg-sky-500 hover:bg-sky-400 text-slate-950 text-xs font-bold transition-colors" id="btnRefreshQr">
            🔄 تازه کردن QR
          </button>
          <button onclick="closeQrModal()" class="px-4 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs transition-colors" id="btnCancelQr">
            بستن
          </button>
        </div>
      </div>

      <!-- Wizard Step 3: Success & Copy MCP Commands -->
      <div id="qrWizardStepSuccess" class="hidden space-y-4 text-right">
        <div class="p-4 rounded-xl bg-emerald-500/10 border border-emerald-500/30 text-center space-y-1.5">
          <div class="text-xl">🎉</div>
          <h4 class="text-base font-bold text-emerald-300" id="successTitle">ورود با موفقیت انجام شد!</h4>
          <p class="text-xs text-slate-300" id="successSubtitle">
            نشست حساب <strong id="successLabelBadge" class="text-sky-300 font-mono px-1.5 py-0.5 rounded bg-slate-900 border border-slate-700"></strong> در فایل <code class="text-emerald-400 font-mono">.env</code> ثبت شد.
          </p>
        </div>

        <div class="space-y-3">
          <div class="flex items-center justify-between">
            <h5 class="text-xs font-bold text-white flex items-center gap-1.5">
              <span>📋</span> <span id="titleCommandsSuccess">دستورات اتصال کلاینت‌های هوش مصنوعی (MCP):</span>
            </h5>
            <span class="text-[10px] text-slate-400" id="tipClickToCopy">روی کپی کلیک کنید</span>
          </div>

          <div id="modalMcpCommandsContainer" class="space-y-3 max-h-72 overflow-y-auto pr-1">
            <!-- Dynamically populated -->
          </div>
        </div>

        <div class="pt-2">
          <button onclick="finishSuccessWizard()" class="w-full py-2.5 rounded-xl bg-sky-500 hover:bg-sky-400 text-slate-950 font-bold text-xs transition-colors" id="btnDoneWizard">
            ✅ اتمام و بازگشت به داشبورد
          </button>
        </div>
      </div>

    </div>
  </div>

  <script>
    let currentConfig = {};
    let currentStatus = {};
    let selectedAccount = localStorage.getItem('safe_telegram_selected_acc') || 'default';
    let qrPollInterval = null;
    let currentLang = localStorage.getItem('safe_telegram_lang') || 'fa';

    const I18N = {
      fa: {
        badgeHardened: "امن‌سازی شده",
        headerSubtitle: "لیست سفید چت‌ها، جلوگیری از نشت داده و ضدبن هوشمند تلگرام",
        langBtnText: "English",
        btnApiKeys: "کلیدهای API",
        btnQrLogin: "ورود QR",
        titleWhitelist: "چت‌های مجاز (Whitelist)",
        btnExport: "خروجی",
        btnImport: "ورودی",
        descWhitelist: "هوش مصنوعی فقط به چت‌های این لیست دسترسی دارد. کلیه پیام‌های شخصی، خانوادگی، بانکی و سایر گروه‌ها کاملاً مسدود و غیرقابل خواندن هستند.",
        btnAdd: "افزودن",
        tipSavedMessages: "💡 نکته: با افزودن me هوش مصنوعی فقط به پیام‌های ذخیره‌شده (Saved Messages) دسترسی خواهد داشت.",
        titlePolicies: "سیاست‌های امنیتی و ضدبن",
        optSafeMode: "حالت امن (Safe Mode)",
        descSafeMode: "اجبار محدودیت‌های لیست سفید و تاخیر ضداسپم",
        optReadOnly: "حالت فقط خواندنی (Read-Only)",
        descReadOnly: "مسدودسازی کامل ارسال، ویرایش یا حذف پیام‌ها",
        optPeerFlood: "مهار پیام به غریبه‌ها (Anti-Peer Flood)",
        descPeerFlood: "جلوگیری از ارسال پیام به شماره‌های خارج از مخاطبین",
        optDelay: "وقفه بین درخواست‌ها (Delay)",
        labelAggressive: "سریع (0.5s)",
        labelRecommended: "پیشنهادی (1.5s - 2.0s)",
        labelUltraSafe: "فوق امن (5.0s)",
        placeholderInput: "مثال: me یا mychannel@ یا 12345678",
        statusConnected: "تلگرام متصل شد",
        statusApiSet: "کلیدها ثبت شد (اسکن QR)",
        statusMissingApi: "ثبت کلیدهای API",
        chatSingular: "چت",
        chatPlural: "چت",
        noChats: "هنوز چتی به لیست مجاز اضافه نشده است.",
        quickSetupTitle: "⚡ راهکار فوری بدون دردسر:",
        btnFillDesktopKeys: "استفاده از کلیدهای رسمی دسکتاپ",
        quickSetupDesc: "اگر در سایت my.telegram.org خطای ERROR دریافت می‌کنید، با زدن این دکمه از کلیدهای متن‌باز تلگرام دسکتاپ (2040) استفاده کنید و مستقیم وارد شوید.",
        titleAccounts: "حساب‌های تلگرام (Telegram Accounts)",
        btnNewAccount: "+ افزودن حساب جدید",
        descAccounts: "امکان اتصال همزمان چندین اکانت وجود دارد. هوش مصنوعی با پارامتر account='برچسب' بین اکانت‌ها سوییچ می‌کند.",
        titleMcpGuide: "راهنمای اتصال به کلاینت‌های MCP",
        descMcpGuide: "پس از لاگین، دستور متناسب با ابزار AI خود را با یک کلیک کپی کرده و اجرا کنید:",
        lblAccountLabel: "🏷️ نام برچسب حساب (Account Label):",
        lblAccountLabelDesc: "برای حساب اصلی عبارت default و برای سایر حساب‌ها یک نام انگلیسی دلخواه (مانند work یا second) وارد کنید.",
        btnRefreshQr: "🔄 تازه کردن QR",
        btnCancelQr: "بستن",
        successTitle: "ورود با موفقیت انجام شد!",
        titleCommandsSuccess: "دستورات اتصال کلاینت‌های هوش مصنوعی (MCP):",
        tipClickToCopy: "روی کپی کلیک کنید",
        btnDoneWizard: "✅ اتمام و بازگشت به داشبورد",
      },
      en: {
        badgeHardened: "Hardened",
        headerSubtitle: "Zero-Leak Chat Whitelisting & Anti-Flood Ban Protection",
        langBtnText: "فارسی",
        btnApiKeys: "API Keys",
        btnQrLogin: "QR Login",
        titleWhitelist: "Allowed Chats (Whitelist)",
        btnExport: "Export",
        btnImport: "Import",
        descWhitelist: "AI agents can ONLY read, query, or send to approved targets. All private family chats, banks, and unlisted groups remain invisible and strictly forbidden.",
        btnAdd: "Add",
        tipSavedMessages: "💡 Tip: Use me to restrict AI exclusively to your own Saved Messages.",
        titlePolicies: "Security & Anti-Flood Policies",
        optSafeMode: "Safe Mode Protection",
        descSafeMode: "Enforces whitelist barriers & rate controls",
        optReadOnly: "Read-Only Mode",
        descReadOnly: "Blocks sending messages, edits, or deletes",
        optPeerFlood: "Anti-Peer Flood Guard",
        descPeerFlood: "Blocks sending messages to non-contact strangers",
        optDelay: "Request Pacing (Delay)",
        labelAggressive: "Aggressive (0.5s)",
        labelRecommended: "Recommended (1.5s - 2.0s)",
        labelUltraSafe: "Ultra-Safe (5.0s)",
        placeholderInput: "e.g. me, @mychannel, or 12345678",
        statusConnected: "Telegram Connected",
        statusApiSet: "API Keys Set (Scan QR)",
        statusMissingApi: "Set API Keys",
        chatSingular: "chat",
        chatPlural: "chats",
        noChats: "No chats whitelisted yet.",
        quickSetupTitle: "⚡ Quick 1-Click Solution:",
        btnFillDesktopKeys: "Use Official Desktop Keys",
        quickSetupDesc: "If my.telegram.org returns 'ERROR', click this button to automatically use official open-source Telegram Desktop credentials (2040) without registration.",
        titleAccounts: "Telegram Accounts (Multi-Account)",
        btnNewAccount: "+ Add New Account",
        descAccounts: "Connect multiple Telegram accounts simultaneously. AI tools switch accounts using account='label'.",
        titleMcpGuide: "MCP Client Connection Commands",
        descMcpGuide: "After login, copy and run the snippet for your preferred AI client with one click:",
        lblAccountLabel: "🏷️ Account Label:",
        lblAccountLabelDesc: "Use default for the primary account, or any unique name (e.g. work, personal) for secondary accounts.",
        btnRefreshQr: "🔄 Refresh QR",
        btnCancelQr: "Close",
        successTitle: "Authentication Successful!",
        titleCommandsSuccess: "AI Client Connection Snippets (MCP):",
        tipClickToCopy: "Click copy button",
        btnDoneWizard: "✅ Finish & Back to Dashboard",
      }
    };

    function applyLanguage(lang) {
      currentLang = lang;
      localStorage.setItem('safe_telegram_lang', lang);
      const dict = I18N[lang];
      const html = document.documentElement;

      if (lang === 'fa') {
        html.setAttribute('dir', 'rtl');
        html.setAttribute('lang', 'fa');
      } else {
        html.setAttribute('dir', 'ltr');
        html.setAttribute('lang', 'en');
      }

      for (const [key, val] of Object.entries(dict)) {
        const el = document.getElementById(key);
        if (el) el.textContent = val;
      }

      const input = document.getElementById('newChatInput');
      if (input) input.placeholder = dict.placeholderInput;

      renderWhitelist(currentConfig.allowed_chats || []);
      loadStatus();
    }

    function toggleLang() {
      applyLanguage(currentLang === 'fa' ? 'en' : 'fa');
    }

    function togglePlaybook() {
      const body = document.getElementById('playbookBody');
      const chevron = document.getElementById('playbookChevron');
      if (body.classList.contains('hidden')) {
        body.classList.remove('hidden');
        chevron.textContent = '▼';
      } else {
        body.classList.add('hidden');
        chevron.textContent = '▶';
      }
    }

    function openCredentialsModal() {
      document.getElementById('credentialsModal').classList.remove('hidden');
    }

    function closeCredentialsModal() {
      document.getElementById('credentialsModal').classList.add('hidden');
    }

    function useOfficialDesktopKeys() {
      document.getElementById('apiIdInput').value = "2040";
      document.getElementById('apiHashInput').value = "b18441a1ff607e10a989891a5462e627";
      saveCredentials();
    }

    async function saveCredentials() {
      const apiId = document.getElementById('apiIdInput').value.trim();
      const apiHash = document.getElementById('apiHashInput').value.trim();

      if (!apiId || !apiHash) {
        alert(currentLang === 'fa' ? 'لطفاً هر دو مقدار API ID و API Hash را وارد کنید.' : 'Please provide both API ID and API Hash.');
        return;
      }

      try {
        const res = await fetch('/api/credentials', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ api_id: apiId, api_hash: apiHash })
        });
        const data = await res.json();
        if (data.status === 'ok') {
          closeCredentialsModal();
          loadStatus();
          alert(currentLang === 'fa' ? 'کلیدها با موفقیت در فایل .env ذخیره شدند!' : 'Credentials saved successfully to .env!');
        }
      } catch (err) {
        alert('Error: ' + err);
      }
    }

    function renderAccountTabs() {
      const container = document.getElementById('accountTabsContainer');
      if (!container) return;
      const accounts = currentStatus.accounts || [];

      let html = `
        <button onclick="selectAccount('global')" class="px-2.5 py-1.5 rounded-xl text-xs font-semibold transition-all ${selectedAccount === 'global' ? 'bg-sky-500 text-slate-950 shadow-md shadow-sky-500/20 font-bold' : 'bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700'}">
          🌐 عمومی (Global)
        </button>
      `;

      accounts.forEach(acc => {
        const isSel = selectedAccount === acc.label;
        const name = acc.name || acc.label;
        html += `
          <button onclick="selectAccount('${acc.label}')" class="px-2.5 py-1.5 rounded-xl text-xs font-semibold transition-all flex items-center gap-1.5 ${isSel ? 'bg-sky-500 text-slate-950 shadow-md shadow-sky-500/20 font-bold' : 'bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700'}">
            <span>👤</span>
            <span>${name}</span>
            <span class="font-mono text-[10px] opacity-80">(${acc.label})</span>
          </button>
        `;
      });

      container.innerHTML = html;

      // Update badge
      const badge = document.getElementById('activeAccountLabelBadge');
      if (badge) {
        if (selectedAccount === 'global') {
          badge.textContent = 'تنظیمات عمومی (اعمال روی همه)';
        } else {
          const found = accounts.find(a => a.label === selectedAccount);
          const name = found ? (found.name || found.username || found.label) : selectedAccount;
          badge.textContent = `حساب: ${name} (${selectedAccount})`;
        }
      }
    }

    function selectAccount(label, scroll = false) {
      selectedAccount = label || 'default';
      localStorage.setItem('safe_telegram_selected_acc', selectedAccount);
      renderAccountTabs();
      renderAccounts(currentStatus.accounts || []);
      loadConfig(selectedAccount);

      if (scroll) {
        const grid = document.getElementById('settingsGrid');
        if (grid) grid.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    }

    async function loadConfig(account) {
      account = account || selectedAccount || 'default';
      try {
        const res = await fetch('/api/config?account=' + encodeURIComponent(account));
        const data = await res.json();
        currentConfig = data;

        document.getElementById('toggleSafeMode').checked = data.safe_mode;
        document.getElementById('toggleReadOnly').checked = data.read_only;
        document.getElementById('togglePeerFlood').checked = data.prevent_peer_flood;
        document.getElementById('rateLimitInput').value = data.rate_limit_delay;
        document.getElementById('rateLimitLabel').textContent = data.rate_limit_delay + 's';

        renderWhitelist(data.allowed_chats || []);
      } catch (err) {
        console.error('Failed to load config:', err);
      }
    }

    async function loadStatus() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();
        const dot = document.getElementById('statusDot');
        const text = document.getElementById('statusText');
        const dict = I18N[currentLang];

        if (data.api_id) document.getElementById('apiIdInput').value = data.api_id;
        if (data.api_hash) document.getElementById('apiHashInput').value = data.api_hash;

        currentStatus = data;
        renderAccounts(data.accounts || []);
        renderAccountTabs();
        
        const mcpCardsContainer = document.getElementById('mcpCardsContainer');
        if (mcpCardsContainer && data.mcp_configs) {
          mcpCardsContainer.innerHTML = generateMcpSnippetsHtml(data.mcp_configs, 'main');
        }

        if (data.configured) {
          dot.className = "w-2.5 h-2.5 rounded-full bg-emerald-400 shadow-sm shadow-emerald-400/50";
          text.textContent = dict.statusConnected;
          text.className = "text-emerald-300";
        } else if (data.has_api_credentials) {
          dot.className = "w-2.5 h-2.5 rounded-full bg-sky-400";
          text.textContent = dict.statusApiSet;
          text.className = "text-sky-300";
        } else {
          dot.className = "w-2.5 h-2.5 rounded-full bg-amber-400";
          text.textContent = dict.statusMissingApi;
          text.className = "text-amber-300";
        }
      } catch {
        // ignore
      }
    }

    function renderWhitelist(chats) {
      const container = document.getElementById('whitelistContainer');
      const countLabel = document.getElementById('whitelistCount');
      const dict = I18N[currentLang];
      const countText = chats.length === 1 ? dict.chatSingular : dict.chatPlural;
      countLabel.textContent = chats.length + ' ' + countText;

      if (!chats.length) {
        container.innerHTML = `<li class="text-xs text-slate-500 py-3 text-center border border-dashed border-slate-800 rounded-lg">${dict.noChats}</li>`;
        return;
      }

      container.innerHTML = chats.map(chat => `
        <li class="flex items-center justify-between px-3.5 py-2.5 rounded-xl bg-slate-950 border border-slate-800/80 hover:border-slate-700 transition-colors">
          <div class="flex items-center gap-2">
            <span class="text-xs text-sky-400">${chat === 'me' ? '💬' : '📌'}</span>
            <span class="text-sm font-mono text-slate-200">${chat === 'me' ? (currentLang === 'fa' ? 'me (پیام‌های ذخیره‌شده)' : 'me (Saved Messages)') : chat}</span>
          </div>
          <button onclick="removeChat('${chat}')" class="text-xs text-slate-500 hover:text-rose-400 p-1 rounded transition-colors">
            ✕
          </button>
        </li>
      `).join('');
    }

    async function addChat() {
      const input = document.getElementById('newChatInput');
      const val = input.value.trim();
      if (!val) return;

      try {
        const res = await fetch('/api/whitelist/add', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ chat: val, account: selectedAccount })
        });
        const updated = await res.json();
        currentConfig.allowed_chats = updated.allowed_chats;
        renderWhitelist(updated.allowed_chats);
        input.value = '';
      } catch (err) {
        alert('Failed: ' + err);
      }
    }

    async function removeChat(chat) {
      try {
        const res = await fetch('/api/whitelist/remove', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ chat, account: selectedAccount })
        });
        const updated = await res.json();
        currentConfig.allowed_chats = updated.allowed_chats;
        renderWhitelist(updated.allowed_chats);
      } catch (err) {
        alert('Failed: ' + err);
      }
    }

    async function saveConfig() {
      const payload = {
        safe_mode: document.getElementById('toggleSafeMode').checked,
        read_only: document.getElementById('toggleReadOnly').checked,
        prevent_peer_flood: document.getElementById('togglePeerFlood').checked,
        rate_limit_delay: parseFloat(document.getElementById('rateLimitInput').value),
        account: selectedAccount,
      };

      try {
        const res = await fetch('/api/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        currentConfig = data;
      } catch (err) {
        console.error('Failed to save config:', err);
      }
    }

    function exportWhitelist() {
      window.location.href = '/api/whitelist/export?account=' + encodeURIComponent(selectedAccount);
    }

    async function importWhitelist(event) {
      const file = event.target.files[0];
      if (!file) return;

      const reader = new FileReader();
      reader.onload = async (e) => {
        try {
          const content = JSON.parse(e.target.result);
          const chats = Array.isArray(content) ? content : content.allowed_chats;
          if (!Array.isArray(chats)) throw new Error('Invalid JSON format');

          const res = await fetch('/api/whitelist/import', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ allowed_chats: chats, account: selectedAccount })
          });
          const updated = await res.json();
          currentConfig.allowed_chats = updated.allowed_chats;
          renderWhitelist(updated.allowed_chats);
          alert(currentLang === 'fa' ? 'لیست سفید با موفقیت بارگذاری شد!' : 'Whitelist imported successfully!');
        } catch (err) {
          alert('Error: ' + err.message);
        }
      };
      reader.readAsText(file);
    }

    function updateLabelKeyPreview(val) {
      val = (val || '').trim();
      const preview = document.getElementById('lblKeyPreview');
      if (!val || val.toLowerCase() === 'default') {
        preview.textContent = 'TELEGRAM_SESSION_STRING';
      } else {
        const clean = val.replace(/[^A-Za-z0-9_]/g, '_').toUpperCase();
        preview.textContent = 'TELEGRAM_SESSION_STRING_' + clean;
      }
    }

    function openQrModal(mode) {
      document.getElementById('qrWizardStepScan').classList.remove('hidden');
      document.getElementById('qrWizardStepSuccess').classList.add('hidden');
      
      const labelInput = document.getElementById('accountLabelInput');
      if (mode === 'new') {
        const existing = (currentStatus.accounts || []).map(a => a.label.toLowerCase());
        let candidate = 'account2';
        let idx = 2;
        while (existing.includes(candidate)) {
          idx++;
          candidate = 'account' + idx;
        }
        labelInput.value = candidate;
      } else if (!labelInput.value) {
        labelInput.value = 'default';
      }
      updateLabelKeyPreview(labelInput.value);

      document.getElementById('qrModal').classList.remove('hidden');
      startQrAuth();
    }

    function closeQrModal() {
      document.getElementById('qrModal').classList.add('hidden');
      if (qrPollInterval) {
        clearInterval(qrPollInterval);
        qrPollInterval = null;
      }
    }

    function finishSuccessWizard() {
      closeQrModal();
      loadStatus();
    }

    async function startQrAuth() {
      const spinner = document.getElementById('qrSpinner');
      const img = document.getElementById('qrImage');
      const statusText = document.getElementById('qrStatusText');
      const label = document.getElementById('accountLabelInput').value.trim() || 'default';

      spinner.classList.remove('hidden');
      spinner.textContent = currentLang === 'fa' ? "در حال اتصال به MTProto..." : "Connecting to Telegram MTProto...";
      img.classList.add('hidden');
      statusText.textContent = currentLang === 'fa' ? "در حال تولید کد QR امن..." : "Generating secure QR code...";

      if (qrPollInterval) clearInterval(qrPollInterval);

      try {
        const res = await fetch('/api/auth/qr/start', { 
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ label: label })
        });
        if (!res.ok) {
          const raw = await res.text();
          spinner.textContent = "Server Error (" + res.status + "): " + raw.slice(0, 120);
          return;
        }
        const data = await res.json();

        if (data.status === 'error') {
          spinner.textContent = "Error: " + data.message;
          if (data.message.includes("must be set")) {
            setTimeout(() => {
              closeQrModal();
              openCredentialsModal();
            }, 1500);
          }
          return;
        }

        if (data.qr_data_uri) {
          img.src = data.qr_data_uri;
          img.classList.remove('hidden');
          spinner.classList.add('hidden');
          statusText.textContent = (currentLang === 'fa' ? "کد را با تلگرام گوشی قبل از انقضا اسکن کنید: " : "Scan with phone before expiry: ") + (data.expires_at || '');

          qrPollInterval = setInterval(pollQrStatus, 2000);
        }
      } catch (err) {
        spinner.textContent = "Error: " + err;
      }
    }

    async function pollQrStatus() {
      try {
        const res = await fetch('/api/auth/qr/status');
        const data = await res.json();
        const statusText = document.getElementById('qrStatusText');

        if (data.status === 'authenticated') {
          clearInterval(qrPollInterval);
          statusText.className = "text-xs font-bold text-emerald-400";
          statusText.textContent = currentLang === 'fa' ? "✅ ورود موفقیت‌آمیز بود!" : "✅ Successfully Authenticated!";
          
          // Show Step 3: Success Wizard with MCP Client commands!
          setTimeout(() => {
            showSuccessWizard(data);
          }, 800);
        } else if (data.status === 'expired') {
          statusText.textContent = currentLang === 'fa' ? "کد QR منقضی شد. روی تازه کردن کلیک کنید." : "QR expired. Click Refresh QR.";
          clearInterval(qrPollInterval);
        } else if (data.status === 'error') {
          statusText.textContent = "Error: " + data.message;
          clearInterval(qrPollInterval);
        }
      } catch (err) {
        console.error('Error polling QR:', err);
      }
    }

    function showSuccessWizard(data) {
      document.getElementById('qrWizardStepScan').classList.add('hidden');
      document.getElementById('qrWizardStepSuccess').classList.remove('hidden');
      document.getElementById('successLabelBadge').textContent = data.label || 'default';

      const container = document.getElementById('modalMcpCommandsContainer');
      const configs = data.mcp_configs || (currentStatus.mcp_configs || {});
      container.innerHTML = generateMcpSnippetsHtml(configs, 'modal');
    }

    function copySnippet(btnId, textToCopy) {
      navigator.clipboard.writeText(textToCopy).then(() => {
        const btn = document.getElementById(btnId);
        if (!btn) return;
        const orig = btn.innerHTML;
        btn.innerHTML = '✓ کپی شد!';
        btn.classList.remove('bg-sky-500', 'text-slate-950');
        btn.classList.add('bg-emerald-500', 'text-white');
        setTimeout(() => {
          btn.innerHTML = orig;
          btn.classList.remove('bg-emerald-500', 'text-white');
          btn.classList.add('bg-sky-500', 'text-slate-950');
        }, 1800);
      }).catch(err => {
        alert('Copy failed: ' + err);
      });
    }

    function generateMcpSnippetsHtml(configs, prefix) {
      if (!configs) return '';
      const hermesCmd = configs.hermes || '';
      const claudeCfg = configs.claude || '';
      const cursorCfg = configs.cursor || '';
      const codexCmd = configs.codex || '';

      return `
        <!-- Hermes Agent -->
        <div class="bg-slate-950 border border-slate-800 rounded-xl p-3 space-y-2">
          <div class="flex items-center justify-between">
            <span class="text-xs font-bold text-sky-400 flex items-center gap-1.5">
              <span>🤖</span> <span>Hermes Agent CLI</span>
            </span>
            <button id="${prefix}_copy_hermes" onclick="copySnippetBySelector('${prefix}_pre_hermes', '${prefix}_copy_hermes')" class="text-[11px] px-2.5 py-1 bg-sky-500 hover:bg-sky-400 text-slate-950 font-bold rounded-lg transition-colors">
              📋 کپی دستور
            </button>
          </div>
          <pre id="${prefix}_pre_hermes" class="bg-slate-900 border border-slate-800 rounded-lg p-2 text-[11px] font-mono text-slate-300 overflow-x-auto text-left" dir="ltr">${hermesCmd}</pre>
        </div>

        <!-- Claude Desktop -->
        <div class="bg-slate-950 border border-slate-800 rounded-xl p-3 space-y-2">
          <div class="flex items-center justify-between">
            <span class="text-xs font-bold text-amber-400 flex items-center gap-1.5">
              <span>🖥️</span> <span>Claude Desktop (claude_desktop_config.json)</span>
            </span>
            <button id="${prefix}_copy_claude" onclick="copySnippetBySelector('${prefix}_pre_claude', '${prefix}_copy_claude')" class="text-[11px] px-2.5 py-1 bg-sky-500 hover:bg-sky-400 text-slate-950 font-bold rounded-lg transition-colors">
              📋 کپی JSON
            </button>
          </div>
          <pre id="${prefix}_pre_claude" class="bg-slate-900 border border-slate-800 rounded-lg p-2 text-[11px] font-mono text-slate-300 overflow-x-auto text-left" dir="ltr">${claudeCfg}</pre>
        </div>

        <!-- Cursor & Windsurf -->
        <div class="bg-slate-950 border border-slate-800 rounded-xl p-3 space-y-2">
          <div class="flex items-center justify-between">
            <span class="text-xs font-bold text-indigo-400 flex items-center gap-1.5">
              <span>⚡</span> <span>Cursor / Windsurf (.cursor/mcp.json)</span>
            </span>
            <button id="${prefix}_copy_cursor" onclick="copySnippetBySelector('${prefix}_pre_cursor', '${prefix}_copy_cursor')" class="text-[11px] px-2.5 py-1 bg-sky-500 hover:bg-sky-400 text-slate-950 font-bold rounded-lg transition-colors">
              📋 کپی JSON
            </button>
          </div>
          <pre id="${prefix}_pre_cursor" class="bg-slate-900 border border-slate-800 rounded-lg p-2 text-[11px] font-mono text-slate-300 overflow-x-auto text-left" dir="ltr">${cursorCfg}</pre>
        </div>

        <!-- Codex CLI -->
        <div class="bg-slate-950 border border-slate-800 rounded-xl p-3 space-y-2">
          <div class="flex items-center justify-between">
            <span class="text-xs font-bold text-emerald-400 flex items-center gap-1.5">
              <span>💻</span> <span>Codex CLI</span>
            </span>
            <button id="${prefix}_copy_codex" onclick="copySnippetBySelector('${prefix}_pre_codex', '${prefix}_copy_codex')" class="text-[11px] px-2.5 py-1 bg-sky-500 hover:bg-sky-400 text-slate-950 font-bold rounded-lg transition-colors">
              📋 کپی دستور
            </button>
          </div>
          <pre id="${prefix}_pre_codex" class="bg-slate-900 border border-slate-800 rounded-lg p-2 text-[11px] font-mono text-slate-300 overflow-x-auto text-left" dir="ltr">${codexCmd}</pre>
        </div>
      `;
    }

    async function removeAccount(key, label) {
      const confirmMsg = currentLang === 'fa' 
        ? `آیا از حذف حساب '${label}' مطمئن هستید؟` 
        : `Are you sure you want to remove account '${label}'?`;
      if (!confirm(confirmMsg)) return;

      try {
        const res = await fetch('/api/accounts/remove', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ key: key })
        });
        const data = await res.json();
        if (data.status === 'ok') {
          loadStatus();
        } else {
          alert('Failed to remove: ' + data.message);
        }
      } catch (err) {
        alert('Error: ' + err);
      }
    }

    async function checkAccountsLive() {
      const btn = document.getElementById('btnCheckAccounts');
      const btnText = document.getElementById('btnCheckAccountsText');
      if (btn) btn.disabled = true;
      if (btnText) btnText.textContent = currentLang === 'fa' ? 'در حال بررسی...' : 'Checking...';

      try {
        const res = await fetch('/api/accounts/verify', { method: 'POST' });
        const data = await res.json();
        if (data.status === 'ok') {
          currentStatus.accounts = data.accounts;
          renderAccounts(data.accounts);
        }
      } catch (err) {
        console.error('Check failed:', err);
      } finally {
        if (btn) btn.disabled = false;
        if (btnText) btnText.textContent = currentLang === 'fa' ? 'بررسی اتصال' : 'Check Status';
      }
    }

    function openEditAccountModal(key, label) {
      document.getElementById('editOldKey').value = key;
      document.getElementById('editLabelInput').value = label;
      document.getElementById('editKeyPreview').value = key;
      document.getElementById('editAccountModal').classList.remove('hidden');
    }

    function closeEditAccountModal() {
      document.getElementById('editAccountModal').classList.add('hidden');
    }

    async function submitEditAccount() {
      const oldKey = document.getElementById('editOldKey').value;
      const newLabel = document.getElementById('editLabelInput').value.trim();
      if (!newLabel) {
        alert(currentLang === 'fa' ? 'لطفاً نام برچسب را وارد کنید.' : 'Please provide a label name.');
        return;
      }

      try {
        const res = await fetch('/api/accounts/edit', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ old_key: oldKey, new_label: newLabel })
        });
        const data = await res.json();
        if (data.status === 'ok') {
          closeEditAccountModal();
          currentStatus.accounts = data.accounts;
          renderAccounts(data.accounts);
          loadStatus();
        } else {
          alert('Error: ' + data.message);
        }
      } catch (err) {
        alert('Error: ' + err);
      }
    }

    function renderAccounts(accounts) {
      const container = document.getElementById('accountsContainer');
      const badge = document.getElementById('accountsCountBadge');
      accounts = accounts || [];
      badge.textContent = accounts.length + (currentLang === 'fa' ? ' حساب' : ' account(s)');

      if (!accounts.length) {
        container.innerHTML = `<div class="text-xs text-slate-500 py-4 text-center border border-dashed border-slate-800 rounded-xl md:col-span-2">
          ${currentLang === 'fa' ? 'هنوز هیچ حساب تلگرامی متصل نشده است.' : 'No Telegram accounts connected yet.'}
        </div>`;
        return;
      }

      container.innerHTML = accounts.map(acc => {
        const isRevoked = acc.status === 'revoked';
        const isActive = acc.status === 'active';
        const isUnknown = !isActive && !isRevoked;

        let statusBadge = '';
        if (isActive) {
          statusBadge = '<span class="text-[10px] px-2 py-0.5 rounded-full bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 font-medium">🟢 متصل</span>';
        } else if (isRevoked) {
          statusBadge = '<span class="text-[10px] px-2 py-0.5 rounded-full bg-rose-500/20 text-rose-300 border border-rose-500/30 font-medium">🔴 باطل‌شده در تلگرام</span>';
        } else {
          statusBadge = '<span class="text-[10px] px-2 py-0.5 rounded-full bg-amber-500/20 text-amber-300 border border-amber-500/30 font-medium">🟡 نیاز به بررسی</span>';
        }

        const displayName = acc.name || (isActive ? 'کاربر تلگرام' : (isRevoked ? 'نشست لغو شده' : 'حساب بدون نام'));
        const usernameTag = acc.username ? `<span class="text-sky-400 font-mono text-xs">@${acc.username}</span>` : '';
        const idTag = acc.user_id ? `<span class="text-slate-400 text-[11px] font-mono">ID: ${acc.user_id}</span>` : '';
        const phoneTag = acc.phone ? `<span class="text-slate-400 text-[11px] font-mono">${acc.phone}</span>` : '';
        const typeBadge = acc.type === 'file' ? '<span class="text-[9px] px-1 py-0.2 rounded bg-slate-800 text-slate-400">File (.session)</span>' : '<span class="text-[9px] px-1 py-0.2 rounded bg-sky-950 text-sky-400 border border-sky-800/40">StringSession</span>';

        const isSelected = selectedAccount === acc.label;
        const borderClass = isSelected 
          ? 'border-sky-500 ring-2 ring-sky-500/50 bg-slate-900/80 shadow-lg shadow-sky-500/10' 
          : (isRevoked ? 'border-rose-900/60 bg-rose-950/10 hover:border-rose-700' : 'border-slate-800 hover:border-slate-700 bg-slate-950');

        return `
          <div onclick="selectAccount('${acc.label}', true)" class="p-4 rounded-xl ${borderClass} transition-all space-y-3 cursor-pointer relative group">
            ${isSelected ? '<div class="absolute -top-2.5 left-4 px-2 py-0.5 rounded-full bg-sky-500 text-slate-950 text-[10px] font-bold shadow-md">✓ حساب فعال در فرم</div>' : ''}
            <div class="flex items-start justify-between gap-2">
              <div class="flex items-center gap-2.5">
                <div class="w-9 h-9 rounded-xl ${isRevoked ? 'bg-rose-500/10 text-rose-400 border border-rose-500/30' : 'bg-sky-500/10 text-sky-400 border border-sky-500/30'} flex items-center justify-center font-bold text-base shadow-inner">
                  ${isRevoked ? '⚠️' : '👤'}
                </div>
                <div>
                  <div class="flex items-center gap-1.5 flex-wrap">
                    <span class="text-sm font-bold text-white">${displayName}</span>
                    ${usernameTag}
                    ${typeBadge}
                  </div>
                  <div class="flex items-center gap-2 mt-0.5 text-slate-400">
                    ${idTag}
                    ${phoneTag ? `<span>•</span> ${phoneTag}` : ''}
                  </div>
                </div>
              </div>

              <div class="flex items-center gap-1">
                <button onclick="event.stopPropagation(); openEditAccountModal('${acc.key}', '${acc.label}')" class="text-xs text-slate-400 hover:text-white p-1.5 rounded-lg hover:bg-slate-800 transition-colors" title="ویرایش برچسب">
                  ✏️
                </button>
                <button onclick="event.stopPropagation(); removeAccount('${acc.key}', '${acc.label}')" class="text-xs text-slate-400 hover:text-rose-400 p-1.5 rounded-lg hover:bg-rose-500/10 transition-colors" title="حذف نشست">
                  🗑️
                </button>
              </div>
            </div>

            <!-- Details & Status Row -->
            <div class="pt-2 border-t border-slate-800/80 flex items-center justify-between gap-2 flex-wrap text-xs">
              <div class="flex items-center gap-2">
                <span class="text-slate-400 text-[11px]">برچسب:</span>
                <span class="font-mono text-sky-300 font-bold bg-slate-900 px-2 py-0.5 rounded border border-slate-800">${acc.label}</span>
                ${statusBadge}
              </div>
              <div class="text-[10px] text-slate-500 font-mono">${acc.key}</div>
            </div>

            ${isRevoked ? `
              <div class="p-2 rounded-lg bg-rose-500/10 border border-rose-500/20 text-[11px] text-rose-300 leading-relaxed flex items-center justify-between gap-2">
                <span>⚠️ این نشست از اپلیکیشن گوشی بسته شده و دیگر معتبر نیست.</span>
                <button onclick="event.stopPropagation(); removeAccount('${acc.key}', '${acc.label}')" class="text-[10px] px-2 py-1 bg-rose-500/20 hover:bg-rose-500/40 text-rose-200 rounded border border-rose-500/30 whitespace-nowrap">
                  حذف این نشست
                </button>
              </div>
            ` : ''}
          </div>
        `;
      }).join('');
    }

    applyLanguage(currentLang);
    loadConfig();
    loadStatus();
  </script>
</body>
</html>
"""


async def get_dashboard(request: Request) -> HTMLResponse:
    return HTMLResponse(HTML_TEMPLATE)


async def api_get_config(request: Request) -> JSONResponse:
    acc = request.query_params.get("account")
    cfg = get_security_config(account=acc)
    return JSONResponse({
        "safe_mode": cfg.safe_mode,
        "read_only": cfg.read_only,
        "rate_limit_delay": cfg.rate_limit_delay,
        "max_messages_limit": cfg.max_messages_limit,
        "allowed_chats": cfg.allowed_chats,
        "prevent_peer_flood": cfg.prevent_peer_flood,
        "strict_whitelist": cfg.strict_whitelist,
        "account": acc or "global",
    })


async def api_post_config(request: Request) -> JSONResponse:
    data = await request.json()
    acc = str(data.get("account") or request.query_params.get("account") or "").strip().lower()
    cfg = get_security_config()
    is_per_account = bool(acc and acc not in ("global", "all", "none"))
    target_dict = cfg.accounts.setdefault(acc, {}) if is_per_account else None

    def set_val(field_name: str, val: Any):
        if target_dict is not None:
            target_dict[field_name] = val
        else:
            setattr(cfg, field_name, val)

    if "safe_mode" in data:
        set_val("safe_mode", bool(data["safe_mode"]))
    if "read_only" in data:
        set_val("read_only", bool(data["read_only"]))
    if "rate_limit_delay" in data:
        set_val("rate_limit_delay", float(data["rate_limit_delay"]))
    if "max_messages_limit" in data:
        set_val("max_messages_limit", int(data["max_messages_limit"]))
    if "prevent_peer_flood" in data:
        set_val("prevent_peer_flood", bool(data["prevent_peer_flood"]))
    if "strict_whitelist" in data:
        set_val("strict_whitelist", bool(data["strict_whitelist"]))

    update_security_config(cfg)
    return JSONResponse({"status": "ok", "account": acc or "global", "config": data})


async def api_whitelist_add(request: Request) -> JSONResponse:
    data = await request.json()
    chat = str(data.get("chat", "")).strip()
    acc = str(data.get("account") or "").strip().lower()
    cfg = get_security_config()
    is_per_account = bool(acc and acc not in ("global", "all", "none"))

    if is_per_account:
        target = cfg.accounts.setdefault(acc, {})
        if "allowed_chats" not in target:
            target["allowed_chats"] = list(cfg.allowed_chats)
        chats_list = target["allowed_chats"]
    else:
        chats_list = cfg.allowed_chats

    if chat and chat not in chats_list:
        chats_list.append(chat)
        update_security_config(cfg)
    return JSONResponse({"status": "ok", "allowed_chats": chats_list, "account": acc or "global"})


async def api_whitelist_remove(request: Request) -> JSONResponse:
    data = await request.json()
    chat = str(data.get("chat", "")).strip()
    acc = str(data.get("account") or "").strip().lower()
    cfg = get_security_config()
    is_per_account = bool(acc and acc not in ("global", "all", "none"))

    if is_per_account:
        target = cfg.accounts.setdefault(acc, {})
        if "allowed_chats" not in target:
            target["allowed_chats"] = list(cfg.allowed_chats)
        chats_list = target["allowed_chats"]
    else:
        chats_list = cfg.allowed_chats

    if chat in chats_list:
        chats_list.remove(chat)
        update_security_config(cfg)
    return JSONResponse({"status": "ok", "allowed_chats": chats_list, "account": acc or "global"})


async def api_whitelist_export(request: Request) -> Response:
    acc = request.query_params.get("account")
    cfg = get_security_config(account=acc)
    data = json.dumps({"allowed_chats": cfg.allowed_chats, "account": acc or "global"}, indent=2)
    filename = f"safe_whitelist_{acc or 'global'}.json"
    return Response(
        content=data,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def api_whitelist_import(request: Request) -> JSONResponse:
    data = await request.json()
    new_chats = data.get("allowed_chats", [])
    acc = str(data.get("account") or "").strip().lower()
    cfg = get_security_config()
    is_per_account = bool(acc and acc not in ("global", "all", "none"))

    if is_per_account:
        target = cfg.accounts.setdefault(acc, {})
        if "allowed_chats" not in target:
            target["allowed_chats"] = list(cfg.allowed_chats)
        chats_list = target["allowed_chats"]
    else:
        chats_list = cfg.allowed_chats

    for c in new_chats:
        c_str = str(c).strip()
        if c_str and c_str not in chats_list:
            chats_list.append(c_str)

    update_security_config(cfg)
    return JSONResponse({"status": "ok", "allowed_chats": chats_list, "account": acc or "global"})


async def api_post_credentials(request: Request) -> JSONResponse:
    data = await request.json()
    api_id = str(data.get("api_id", "")).strip()
    api_hash = str(data.get("api_hash", "")).strip()

    if not api_id or not api_hash:
        return JSONResponse({"status": "error", "message": "Both api_id and api_hash are required"}, status_code=400)

    _safe_write_env_key("TELEGRAM_API_ID", api_id)
    _safe_write_env_key("TELEGRAM_API_HASH", api_hash)

    return JSONResponse({"status": "ok"})


async def api_get_status(request: Request) -> JSONResponse:
    load_dotenv(override=True)
    api_id = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()

    is_dummy_api = (api_id == "123456" or api_hash == "0123456789abcdef0123456789abcdef")
    has_api_credentials = bool(api_id and api_hash and not is_dummy_api)

    session_str = os.getenv("TELEGRAM_SESSION_STRING", "").strip()
    is_dummy_session = session_str.startswith("123123")
    has_session = bool(session_str and len(session_str) > 30 and not is_dummy_session)

    env_exists = Path(".env").is_file()
    verified_accounts = await _verify_all_accounts(force=False)
    safe_accounts = [{k: v for k, v in a.items() if k != "raw_value"} for a in verified_accounts]
    has_any_active = any(a.get("status") == "active" for a in verified_accounts)
    repo_dir = str(Path(__file__).parent.parent.resolve())

    return JSONResponse({
        "configured": has_api_credentials and has_any_active,
        "has_api_credentials": has_api_credentials,
        "api_id": "" if is_dummy_api else api_id,
        "api_hash": "" if is_dummy_api else ((api_hash[:4] + "..." + api_hash[-4:]) if len(api_hash) > 8 else ""),
        "has_env_file": env_exists,
        "safe_config_file": Path(DEFAULT_CONFIG_FILE).is_file(),
        "accounts": safe_accounts,
        "repo_dir": repo_dir,
        "mcp_configs": _generate_mcp_configs(repo_dir),
    })


async def _async_qr_wait(client, qr, env_key: str, display_label: str):
    from telethon import errors

    try:
        await qr.wait(timeout=60.0)
        session_str = client.session.save()
        _safe_write_env_key(env_key, session_str)
        _QR_STATE["status"] = "authenticated"
        _QR_STATE["label"] = display_label
        _QR_STATE["env_key"] = env_key
    except asyncio.TimeoutError:
        _QR_STATE["status"] = "expired"
    except errors.SessionPasswordNeededError:
        _QR_STATE["status"] = "error"
        _QR_STATE["error_message"] = "2FA Password needed. Use session_string_generator.py for 2FA."
    except Exception as exc:
        _QR_STATE["status"] = "error"
        _QR_STATE["error_message"] = str(exc)
    finally:
        await client.disconnect()


async def api_auth_qr_start(request: Request) -> JSONResponse:
    load_dotenv(override=True)
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")

    if not api_id or not api_hash:
        return JSONResponse({
            "status": "error",
            "message": "TELEGRAM_API_ID and TELEGRAM_API_HASH must be set first. Click 'API Keys' button in top bar."
        })

    try:
        api_id_int = int(api_id)
    except ValueError:
        return JSONResponse({"status": "error", "message": "Invalid TELEGRAM_API_ID. It must be an integer."})

    label_input = "default"
    try:
        body = await request.json()
        label_input = str(body.get("label", "default")).strip()
    except Exception:
        pass

    import re
    clean_label = re.sub(r"[^A-Za-z0-9_]", "_", label_input).strip("_")
    if not clean_label or clean_label.lower() == "default":
        env_key = "TELEGRAM_SESSION_STRING"
        display_label = "default"
    else:
        env_key = f"TELEGRAM_SESSION_STRING_{clean_label.upper()}"
        display_label = clean_label.lower()

    kwargs = client_identity_kwargs()
    if api_id_int == 2040:
        kwargs.setdefault("device_model", "Telegram Desktop")
        kwargs.setdefault("system_version", "macOS 15.3")
        kwargs.setdefault("app_version", "5.10.3")

    try:
        from telegram_mcp.proxy import _build_proxy_for_label
        proxy, connection = _build_proxy_for_label(display_label)
        if proxy is not None:
            kwargs["proxy"] = proxy
        if connection is not None:
            kwargs["connection"] = connection
    except Exception:
        pass

    try:
        client = TelegramClient(StringSession(), api_id_int, api_hash, **kwargs)
        await asyncio.wait_for(client.connect(), timeout=15.0)

        qr = await client.qr_login()
        data_uri = _generate_qr_data_uri(qr.url)
        expires_str = qr.expires.astimezone().strftime("%H:%M:%S")

        _QR_STATE["client"] = client
        _QR_STATE["qr"] = qr
        _QR_STATE["status"] = "waiting_scan"
        _QR_STATE["qr_data_uri"] = data_uri
        _QR_STATE["expires_at"] = expires_str
        _QR_STATE["label"] = display_label
        _QR_STATE["env_key"] = env_key
        _QR_STATE["task"] = asyncio.create_task(_async_qr_wait(client, qr, env_key, display_label))

        return JSONResponse({
            "status": "waiting_scan",
            "qr_data_uri": data_uri,
            "qr_url": qr.url,
            "expires_at": expires_str,
            "label": display_label,
            "env_key": env_key,
        })
    except asyncio.TimeoutError:
        return JSONResponse({
            "status": "error",
            "message": "Connection to Telegram timed out. Check your internet or local proxy (TELEGRAM_PROXY_* in .env)."
        })
    except Exception as exc:
        err_msg = str(exc)
        if "ApiIdInvalidError" in type(exc).__name__ or "api_id" in err_msg.lower():
            err_msg = "Invalid API ID or API Hash. Please verify keys from my.telegram.org/apps."
        return JSONResponse({
            "status": "error",
            "message": f"{type(exc).__name__}: {err_msg}"
        })


async def api_auth_qr_status(request: Request) -> JSONResponse:
    repo_dir = str(Path(__file__).parent.parent.resolve())
    return JSONResponse({
        "status": _QR_STATE["status"],
        "message": _QR_STATE.get("error_message", ""),
        "label": _QR_STATE.get("label", "default"),
        "env_key": _QR_STATE.get("env_key", "TELEGRAM_SESSION_STRING"),
        "mcp_configs": _generate_mcp_configs(repo_dir),
    })




async def api_accounts_verify(request: Request) -> JSONResponse:
    verified = await _verify_all_accounts(force=True)
    safe_accounts = [{k: v for k, v in a.items() if k != "raw_value"} for a in verified]
    return JSONResponse({"status": "ok", "accounts": safe_accounts})


async def api_account_edit(request: Request) -> JSONResponse:
    data = await request.json()
    old_key = str(data.get("old_key", "")).strip()
    new_label = str(data.get("new_label", "")).strip()
    new_value = data.get("new_value")

    if not old_key:
        return JSONResponse({"status": "error", "message": "old_key is required"}, status_code=400)

    accounts = _list_env_accounts()
    target = next((a for a in accounts if a["key"] == old_key), None)
    if not target:
        return JSONResponse({"status": "error", "message": f"Account {old_key} not found"}, status_code=404)

    val_to_write = str(new_value).strip() if (new_value is not None and str(new_value).strip()) else target["raw_value"]

    import re
    clean_label = re.sub(r"[^A-Za-z0-9_]", "_", new_label).strip("_")
    is_file = target["type"] == "file"
    if not clean_label or clean_label.lower() == "default":
        new_key = "TELEGRAM_SESSION_NAME" if is_file else "TELEGRAM_SESSION_STRING"
    else:
        new_key = f"TELEGRAM_SESSION_NAME_{clean_label.upper()}" if is_file else f"TELEGRAM_SESSION_STRING_{clean_label.upper()}"

    if new_key != old_key:
        _safe_remove_env_key(old_key)
        _ACCOUNT_INFO_CACHE.pop(old_key, None)

    _safe_write_env_key(new_key, val_to_write)
    _ACCOUNT_INFO_CACHE.pop(new_key, None)
    load_dotenv(override=True)

    verified = await _verify_all_accounts(force=True)
    safe_accounts = [{k: v for k, v in a.items() if k != "raw_value"} for a in verified]
    return JSONResponse({"status": "ok", "accounts": safe_accounts})

async def api_account_remove(request: Request) -> JSONResponse:
    data = await request.json()
    key = str(data.get("key", "")).strip()
    if not key or not (key == "TELEGRAM_SESSION_STRING" or key.startswith("TELEGRAM_SESSION_STRING_") or key.startswith("TELEGRAM_SESSION_NAME")):
        return JSONResponse({"status": "error", "message": "Invalid account key"}, status_code=400)
    _safe_remove_env_key(key)
    load_dotenv(override=True)
    return JSONResponse({"status": "ok", "accounts": _list_env_accounts()})

routes = [
    Route("/", get_dashboard, methods=["GET"]),
    Route("/api/config", api_get_config, methods=["GET"]),
    Route("/api/config", api_post_config, methods=["POST"]),
    Route("/api/whitelist/add", api_whitelist_add, methods=["POST"]),
    Route("/api/whitelist/remove", api_whitelist_remove, methods=["POST"]),
    Route("/api/whitelist/export", api_whitelist_export, methods=["GET"]),
    Route("/api/whitelist/import", api_whitelist_import, methods=["POST"]),
    Route("/api/credentials", api_post_credentials, methods=["POST"]),
    Route("/api/status", api_get_status, methods=["GET"]),
    Route("/api/auth/qr/start", api_auth_qr_start, methods=["POST"]),
    Route("/api/auth/qr/status", api_auth_qr_status, methods=["GET"]),
    Route("/api/accounts/remove", api_account_remove, methods=["POST"]),
    Route("/api/accounts/verify", api_accounts_verify, methods=["GET", "POST"]),
    Route("/api/accounts/edit", api_account_edit, methods=["POST"]),
]

app = Starlette(debug=False, routes=routes)


def run_web_panel(host: str = "127.0.0.1", port: Optional[int] = None) -> None:
    if port is None:
        env_port = os.getenv("TELEGRAM_PANEL_PORT")
        if env_port:
            try:
                port = int(env_port)
            except ValueError:
                port = None

    if port is None:
        port = find_available_port(8080)

    url = f"http://{host}:{port}"
    print(f"\n=======================================================")
    print(f"  🛡️ Safe-Telegram-MCP Web Dashboard")
    print(f"  🔗 URL: {url}")
    print(f"=======================================================\n")

    try:
        webbrowser.open(url)
    except Exception:
        pass

    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    run_web_panel()
