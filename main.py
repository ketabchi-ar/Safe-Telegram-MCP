"""Compatibility entrypoint for the Telegram MCP server.

The implementation lives in the telegram_mcp package. This module keeps the
historic `main` import path and console script target working.
"""

import sys

if "--ui" in sys.argv or "--web" in sys.argv:
    from telegram_mcp.web_panel import run_web_panel
    run_web_panel()
    sys.exit(0)

from telegram_mcp.install_guard import UnsafeInstallationError, assert_safe_distribution

try:
    assert_safe_distribution()
except UnsafeInstallationError as exc:
    raise SystemExit(str(exc)) from None

from telegram_mcp import runtime as _runtime  # noqa: F401  (historic `main._runtime`)
from telegram_mcp.runtime import *
from telegram_mcp.runner import _main, main
from telegram_mcp.tools import *

if __name__ == "__main__":
    main()
