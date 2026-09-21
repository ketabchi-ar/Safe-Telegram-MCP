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

    <!-- Main Grid -->
    <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
      
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

  <!-- QR Login Modal -->
  <div id="qrModal" class="fixed inset-0 bg-slate-950/80 backdrop-blur-sm z-50 flex items-center justify-center p-4 hidden">
    <div class="bg-slate-900 border border-slate-800 rounded-2xl max-w-sm w-full p-6 space-y-4 shadow-2xl relative text-center">
      <button onclick="closeQrModal()" class="absolute top-4 left-4 text-slate-400 hover:text-white text-lg">✕</button>
      
      <h3 class="text-lg font-bold text-white flex items-center justify-center gap-2">
        <span>📲</span> <span id="modalQrTitle">اتصال به تلگرام با QR</span>
      </h3>
      <p class="text-xs text-slate-400 leading-relaxed" id="modalQrDesc">
        کد QR را با اپلیکیشن تلگرام در گوشی خود اسکن کنید:
        <br><strong class="text-slate-200">Settings > Devices > Link Desktop Device</strong>
      </p>

      <div id="qrContainer" class="p-4 bg-white rounded-xl flex items-center justify-center min-h-[220px]">
        <div id="qrSpinner" class="text-xs text-slate-600 animate-pulse">در حال برقراری ارتباط با MTProto...</div>
        <img id="qrImage" class="hidden w-52 h-52 mx-auto" alt="Scan QR" />
      </div>

      <div id="qrStatusText" class="text-xs text-slate-400">منتظر اسکن...</div>

      <div class="flex gap-2 justify-center">
        <button onclick="startQrAuth()" class="px-3 py-1.5 rounded-lg bg-sky-500 hover:bg-sky-400 text-slate-950 text-xs font-bold transition-colors" id="btnRefreshQr">
          تازه کردن QR
        </button>
        <button onclick="closeQrModal()" class="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs transition-colors" id="btnCancelQr">
          بستن
        </button>
      </div>
    </div>
  </div>

  <script>
    let currentConfig = {};
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
        const dict = I18N[currentLang];

        if (data.api_id) document.getElementById('apiIdInput').value = data.api_id;
        if (data.api_hash) document.getElementById('apiHashInput').value = data.api_hash;

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
          body: JSON.stringify({ chat: val })
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
          body: JSON.stringify({ chat })
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
      window.location.href = '/api/whitelist/export';
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
            body: JSON.stringify({ allowed_chats: chats })
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

    function openQrModal() {
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

    async function startQrAuth() {
      const spinner = document.getElementById('qrSpinner');
      const img = document.getElementById('qrImage');
      const statusText = document.getElementById('qrStatusText');

      spinner.classList.remove('hidden');
      spinner.textContent = currentLang === 'fa' ? "در حال اتصال به MTProto..." : "Connecting to Telegram MTProto...";
      img.classList.add('hidden');
      statusText.textContent = currentLang === 'fa' ? "در حال تولید کد QR امن..." : "Generating secure QR code...";

      if (qrPollInterval) clearInterval(qrPollInterval);

      try {
        const res = await fetch('/api/auth/qr/start', { method: 'POST' });
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
          statusText.textContent = currentLang === 'fa' ? "✅ ورود موفقیت‌آمیز بود! نشست در فایل .env ذخیره شد." : "✅ Successfully Authenticated! Session saved to .env.";
          setTimeout(() => {
            closeQrModal();
            loadStatus();
          }, 2000);
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


async def api_whitelist_export(request: Request) -> Response:
    cfg = get_security_config()
    data = json.dumps({"allowed_chats": cfg.allowed_chats}, indent=2)
    return Response(
        content=data,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="safe_whitelist.json"'},
    )


async def api_whitelist_import(request: Request) -> JSONResponse:
    data = await request.json()
    new_chats = data.get("allowed_chats", [])
    cfg = get_security_config()
    for c in new_chats:
        c_str = str(c).strip()
        if c_str and c_str not in cfg.allowed_chats:
            cfg.allowed_chats.append(c_str)
    update_security_config(cfg)
    return JSONResponse({"status": "ok", "allowed_chats": cfg.allowed_chats})


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

    return JSONResponse({
        "configured": has_api_credentials and has_session,
        "has_api_credentials": has_api_credentials,
        "api_id": "" if is_dummy_api else api_id,
        "api_hash": "" if is_dummy_api else ((api_hash[:4] + "..." + api_hash[-4:]) if len(api_hash) > 8 else ""),
        "has_env_file": env_exists,
        "safe_config_file": Path(DEFAULT_CONFIG_FILE).is_file(),
    })


async def _async_qr_wait(client, qr):
    from telethon import errors

    try:
        await qr.wait(timeout=60.0)
        session_str = client.session.save()
        _safe_write_env_key("TELEGRAM_SESSION_STRING", session_str)
        _QR_STATE["status"] = "authenticated"
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

    kwargs = client_identity_kwargs()

    try:
        from telegram_mcp.proxy import _build_proxy_for_label
        proxy, connection = _build_proxy_for_label("")
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
        _QR_STATE["task"] = asyncio.create_task(_async_qr_wait(client, qr))

        return JSONResponse({
            "status": "waiting_scan",
            "qr_data_uri": data_uri,
            "qr_url": qr.url,
            "expires_at": expires_str,
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
    return JSONResponse({
        "status": _QR_STATE["status"],
        "message": _QR_STATE.get("error_message", ""),
    })


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
