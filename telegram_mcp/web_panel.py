"""Safe-Telegram-MCP Web Dashboard.

Provides a user-friendly management interface to:
- Toggle Safe Mode and Read-Only Mode
- Add/Remove whitelisted chats with ease
- Adjust anti-flood rate limits
- Check Telegram connection and session status
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route
import uvicorn

from telegram_mcp.security import (
    DEFAULT_CONFIG_FILE,
    SafeSecurityConfig,
    get_security_config,
    update_security_config,
)

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en" class="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Safe-Telegram-MCP Dashboard</title>
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
    body { background-color: #0f172a; color: #f8fafc; font-family: system-ui, -apple-system, sans-serif; }
  </style>
</head>
<body class="min-h-screen bg-slate-950 text-slate-100 flex flex-col items-center p-4 md:p-8">
  <div class="max-w-4xl w-full space-y-6">
    
    <!-- Header -->
    <header class="flex flex-col md:flex-row justify-between items-start md:items-center bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-xl gap-4">
      <div>
        <div class="flex items-center gap-3">
          <div class="w-10 h-10 rounded-xl bg-sky-500/20 text-sky-400 flex items-center justify-center font-bold text-xl border border-sky-500/30">
            🛡️
          </div>
          <div>
            <h1 class="text-2xl font-black tracking-tight text-white flex items-center gap-2">
              Safe-Telegram-MCP
              <span class="text-xs px-2 py-0.5 rounded-full bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 font-medium">Hardened</span>
            </h1>
            <p class="text-xs text-slate-400">Security Control & Chat Whitelist Dashboard</p>
          </div>
        </div>
      </div>
      <div id="statusBadge" class="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-slate-800 border border-slate-700 text-xs font-medium">
        <span class="w-2.5 h-2.5 rounded-full bg-slate-500 animate-pulse" id="statusDot"></span>
        <span id="statusText">Checking status...</span>
      </div>
    </header>

    <!-- Main Grid -->
    <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
      
      <!-- Whitelist Management -->
      <section class="bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-xl flex flex-col justify-between">
        <div>
          <div class="flex items-center justify-between mb-4">
            <h2 class="text-lg font-bold text-white flex items-center gap-2">
              <span>📋</span> Allowed Chats (Whitelist)
            </h2>
            <span id="whitelistCount" class="text-xs font-mono px-2 py-0.5 rounded bg-slate-800 text-sky-400 border border-slate-700">0 chats</span>
          </div>
          <p class="text-xs text-slate-400 mb-4">
            The AI agent can ONLY read or send to these approved targets. All others are blocked.
          </p>

          <!-- Add chat form -->
          <div class="flex gap-2 mb-4">
            <input 
              id="newChatInput" 
              type="text" 
              placeholder="e.g. me, @channel, or 12345678" 
              class="flex-1 bg-slate-950 border border-slate-800 rounded-xl px-4 py-2 text-sm focus:outline-none focus:border-sky-500 text-white placeholder-slate-500"
              onkeydown="if(event.key==='Enter') addChat()"
            />
            <button 
              onclick="addChat()" 
              class="bg-sky-500 hover:bg-sky-400 text-slate-950 font-semibold px-4 py-2 rounded-xl text-sm transition-colors shadow-lg shadow-sky-500/10">
              Add
            </button>
          </div>

          <!-- List -->
          <ul id="whitelistContainer" class="space-y-2 max-h-60 overflow-y-auto pr-1">
            <li class="text-xs text-slate-500 py-3 text-center">Loading allowed chats...</li>
          </ul>
        </div>
      </section>

      <!-- Security Controls -->
      <section class="bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-xl space-y-6">
        <h2 class="text-lg font-bold text-white flex items-center gap-2">
          <span>⚙️</span> Security & Anti-Flood Policies
        </h2>

        <!-- Safe Mode Toggle -->
        <div class="flex items-center justify-between p-3 rounded-xl bg-slate-950/60 border border-slate-800">
          <div>
            <div class="font-medium text-sm text-slate-200">Safe Mode Protection</div>
            <div class="text-xs text-slate-400">Enforces whitelist & flood protection rules</div>
          </div>
          <label class="relative inline-flex items-center cursor-pointer">
            <input id="toggleSafeMode" type="checkbox" class="sr-only peer" onchange="saveConfig()">
            <div class="w-11 h-6 bg-slate-700 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-sky-500"></div>
          </label>
        </div>

        <!-- Read Only Toggle -->
        <div class="flex items-center justify-between p-3 rounded-xl bg-slate-950/60 border border-slate-800">
          <div>
            <div class="font-medium text-sm text-slate-200">Read-Only Mode</div>
            <div class="text-xs text-slate-400">Blocks sending messages, deleting, or admin edits</div>
          </div>
          <label class="relative inline-flex items-center cursor-pointer">
            <input id="toggleReadOnly" type="checkbox" class="sr-only peer" onchange="saveConfig()">
            <div class="w-11 h-6 bg-slate-700 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-rose-500"></div>
          </label>
        </div>

        <!-- Anti-Peer Flood -->
        <div class="flex items-center justify-between p-3 rounded-xl bg-slate-950/60 border border-slate-800">
          <div>
            <div class="font-medium text-sm text-slate-200">Anti-Peer Flood Protection</div>
            <div class="text-xs text-slate-400">Blocks sending messages to non-contact strangers</div>
          </div>
          <label class="relative inline-flex items-center cursor-pointer">
            <input id="togglePeerFlood" type="checkbox" class="sr-only peer" onchange="saveConfig()">
            <div class="w-11 h-6 bg-slate-700 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-emerald-500"></div>
          </label>
        </div>

        <!-- Rate Limit Slider -->
        <div class="space-y-2 p-3 rounded-xl bg-slate-950/60 border border-slate-800">
          <div class="flex justify-between items-center">
            <span class="text-sm font-medium text-slate-200">Anti-Flood Delay</span>
            <span id="rateLimitLabel" class="text-xs font-mono text-sky-400 bg-sky-500/10 px-2 py-0.5 rounded">1.5s</span>
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
          <div class="text-[11px] text-slate-500 flex justify-between">
            <span>Fast (0.5s)</span>
            <span>Recommended (1.5s)</span>
            <span>Ultra Safe (5.0s)</span>
          </div>
        </div>

      </section>
    </div>

    <!-- Quick Run Info -->
    <footer class="bg-slate-900/60 border border-slate-800/80 rounded-xl p-4 text-xs text-slate-400 flex flex-col md:flex-row justify-between items-center gap-2">
      <div>Settings automatically save to <code class="text-sky-400 bg-slate-950 px-1.5 py-0.5 rounded">safe_config.json</code></div>
      <div>Safe-Telegram-MCP • Hardened Edition</div>
    </footer>

  </div>

  <script>
    let currentConfig = {};

    async function loadConfig() {
      try {
        const res = await fetch('/api/config');
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

        if (data.configured) {
          dot.className = "w-2.5 h-2.5 rounded-full bg-emerald-400 shadow-sm shadow-emerald-400/50";
          text.textContent = "Telegram Configured";
          text.className = "text-emerald-300";
        } else {
          dot.className = "w-2.5 h-2.5 rounded-full bg-amber-400";
          text.textContent = "Credentials Missing (.env)";
          text.className = "text-amber-300";
        }
      } catch {
        // ignore
      }
    }

    function renderWhitelist(chats) {
      const container = document.getElementById('whitelistContainer');
      const countLabel = document.getElementById('whitelistCount');
      countLabel.textContent = chats.length + ' chat' + (chats.length === 1 ? '' : 's');

      if (!chats.length) {
        container.innerHTML = '<li class="text-xs text-slate-500 py-3 text-center border border-dashed border-slate-800 rounded-lg">No chats whitelisted yet.</li>';
        return;
      }

      container.innerHTML = chats.map(chat => `
        <li class="flex items-center justify-between px-3 py-2 rounded-xl bg-slate-950 border border-slate-800/80 hover:border-slate-700 transition-colors">
          <div class="flex items-center gap-2">
            <span class="text-xs text-sky-400">${chat === 'me' ? '💬' : '📌'}</span>
            <span class="text-sm font-mono text-slate-200">${chat === 'me' ? 'me (Saved Messages)' : chat}</span>
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
          body: JSON.stringify({ chat: val })
        });
        const updated = await res.json();
        renderWhitelist(updated.allowed_chats);
        input.value = '';
      } catch (err) {
        alert('Failed to add chat: ' + err);
      }
    }

    async function removeChat(chat) {
      try {
        const res = await fetch('/api/whitelist/remove', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ chat })
        });
        const updated = await res.json();
        renderWhitelist(updated.allowed_chats);
      } catch (err) {
        alert('Failed to remove chat: ' + err);
      }
    }

    async function saveConfig() {
      const payload = {
        safe_mode: document.getElementById('toggleSafeMode').checked,
        read_only: document.getElementById('toggleReadOnly').checked,
        prevent_peer_flood: document.getElementById('togglePeerFlood').checked,
        rate_limit_delay: parseFloat(document.getElementById('rateLimitInput').value),
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

    loadConfig();
    loadStatus();
  </script>
</body>
</html>
"""


async def get_dashboard(request: Request) -> HTMLResponse:
    return HTMLResponse(HTML_TEMPLATE)


async def api_get_config(request: Request) -> JSONResponse:
    cfg = get_security_config()
    return JSONResponse({
        "safe_mode": cfg.safe_mode,
        "read_only": cfg.read_only,
        "rate_limit_delay": cfg.rate_limit_delay,
        "max_messages_limit": cfg.max_messages_limit,
        "allowed_chats": cfg.allowed_chats,
        "prevent_peer_flood": cfg.prevent_peer_flood,
        "strict_whitelist": cfg.strict_whitelist,
    })


async def api_post_config(request: Request) -> JSONResponse:
    data = await request.json()
    cfg = get_security_config()
    if "safe_mode" in data:
        cfg.safe_mode = bool(data["safe_mode"])
    if "read_only" in data:
        cfg.read_only = bool(data["read_only"])
    if "rate_limit_delay" in data:
        cfg.rate_limit_delay = float(data["rate_limit_delay"])
    if "max_messages_limit" in data:
        cfg.max_messages_limit = int(data["max_messages_limit"])
    if "prevent_peer_flood" in data:
        cfg.prevent_peer_flood = bool(data["prevent_peer_flood"])
    if "strict_whitelist" in data:
        cfg.strict_whitelist = bool(data["strict_whitelist"])

    update_security_config(cfg)
    return JSONResponse({"status": "ok", "config": data})


async def api_whitelist_add(request: Request) -> JSONResponse:
    data = await request.json()
    chat = str(data.get("chat", "")).strip()
    cfg = get_security_config()
    if chat and chat not in cfg.allowed_chats:
        cfg.allowed_chats.append(chat)
        update_security_config(cfg)
    return JSONResponse({"status": "ok", "allowed_chats": cfg.allowed_chats})


async def api_whitelist_remove(request: Request) -> JSONResponse:
    data = await request.json()
    chat = str(data.get("chat", "")).strip()
    cfg = get_security_config()
    if chat in cfg.allowed_chats:
        cfg.allowed_chats.remove(chat)
        update_security_config(cfg)
    return JSONResponse({"status": "ok", "allowed_chats": cfg.allowed_chats})


async def api_get_status(request: Request) -> JSONResponse:
    has_api_id = bool(os.getenv("TELEGRAM_API_ID"))
    has_session = bool(
        os.getenv("TELEGRAM_SESSION_STRING") or os.getenv("TELEGRAM_SESSION_NAME")
    )
    env_exists = Path(".env").is_file()

    return JSONResponse({
        "configured": (has_api_id and has_session) or env_exists,
        "has_env_file": env_exists,
        "safe_config_file": Path(DEFAULT_CONFIG_FILE).is_file(),
    })


routes = [
    Route("/", get_dashboard, methods=["GET"]),
    Route("/api/config", api_get_config, methods=["GET"]),
    Route("/api/config", api_post_config, methods=["POST"]),
    Route("/api/whitelist/add", api_whitelist_add, methods=["POST"]),
    Route("/api/whitelist/remove", api_whitelist_remove, methods=["POST"]),
    Route("/api/status", api_get_status, methods=["GET"]),
]

app = Starlette(debug=False, routes=routes)


def run_web_panel(host: str = "127.0.0.1", port: int = 8080) -> None:
    print(f"\\n🛡️ Safe-Telegram-MCP Web Dashboard starting at http://{host}:{port}\\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    port = int(os.getenv("TELEGRAM_PANEL_PORT", "8080"))
    run_web_panel(port=port)
