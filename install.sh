#!/usr/bin/env bash
set -e

echo ""
echo "======================================================"
echo "   🛡️  Installing Safe-Telegram-MCP (Hardened)      "
echo "======================================================"
echo ""

# Check for Python 3.11+
if command -v python3 >/dev/null 2>&1; then
    PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    echo "✓ Found Python: $PY_VER"
else
    echo "✗ Python 3 not found. Please install Python 3.11 or higher."
    exit 1
fi

# Check for uv (fast package manager)
if ! command -v uv >/dev/null 2>&1; then
    echo "Installing uv package manager..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

echo "✓ Using uv: $(uv --version)"

# Sync dependencies
echo "Syncing virtual environment and dependencies..."
uv sync

# Prepare .env from .env.example if missing
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        echo "Creating .env from .env.example..."
        cp .env.example .env
        chmod 600 .env
        echo "✓ Created .env with private permissions (0600)."
    fi
fi

# Prepare safe_config.json if missing
if [ ! -f safe_config.json ]; then
    if [ -f safe_config.example.json ]; then
        cp safe_config.example.json safe_config.json
        echo "✓ Created safe_config.json."
    fi
fi

echo ""
echo "======================================================"
echo "   ✅ Safe-Telegram-MCP successfully installed!       "
echo "======================================================"
echo ""
echo "Next steps:"
echo " 1. Configure your TELEGRAM_API_ID and TELEGRAM_API_HASH in .env"
echo " 2. Launch the Web Security Dashboard:"
echo "      uv run python main.py --ui"
echo "    (or open http://localhost:8080 in your browser)"
echo ""
