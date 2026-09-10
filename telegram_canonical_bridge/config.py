"""Agent Task Bridge 的共用資料路徑。

本模組刻意不讀取任何 Telegram 或 Hermes backend 秘密。使用者訊息入口由
Hermes 原生 platform adapter 管理；外掛只需要所有本機 profiles 可共用的
SQLite ledger。
"""

from __future__ import annotations

import os
from pathlib import Path


STATE_PATH_ENV = "HERMES_AGENT_TASK_STATE_PATH"
PLUGIN_STATE_DIRECTORY = "telegram-canonical-bridge"
STATE_FILENAME = "tasks.sqlite3"


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        configured = os.getenv("HERMES_HOME", "").strip()
        if configured:
            return Path(configured)
        return Path.home() / ".hermes"


def hermes_machine_root() -> Path:
    """取得 default 與 named profiles 共用的 Hermes machine root。"""

    try:
        from hermes_constants import get_default_hermes_root

        return Path(get_default_hermes_root())
    except Exception:
        home = _hermes_home()
        try:
            from tools.bot_mode_probe import _hermes_root

            return Path(_hermes_root(home))
        except Exception:
            return home.parent.parent if home.parent.name == "profiles" else home


def shared_state_path() -> Path:
    """回傳 v0.6 sidecar 專用 ledger；不沿用 legacy ``bridge.sqlite3``。"""

    configured = os.getenv(STATE_PATH_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return hermes_machine_root() / "plugin-data" / PLUGIN_STATE_DIRECTORY / STATE_FILENAME
