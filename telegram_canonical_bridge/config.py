"""外掛設定與秘密讀取。

非秘密值只讀取 ``platforms.telegram_canonical_bridge.extra``；
Bot token 與 Hermes 後端 token 只從 Hermes 的秘密範圍讀取。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


BOT_TOKEN_ENV = "TELEGRAM_CANONICAL_BRIDGE_BOT_TOKEN"
BACKEND_TOKEN_ENV = "TELEGRAM_CANONICAL_BRIDGE_BACKEND_TOKEN"
STATE_PATH_ENV = "TELEGRAM_CANONICAL_BRIDGE_STATE_PATH"
PLUGIN_STATE_DIRECTORY = "telegram-canonical-bridge"


class BridgeConfigurationError(ValueError):
    """設定不足或格式不安全時使用的例外。"""


def _scoped_secret(name: str) -> str:
    """在 multiplex profile 下保持 fail-closed 的秘密讀取。

    Hermes 現行平台 adapter 使用 ``get_scoped_secret``，它在已安裝的秘密範圍
    找不到值時不會誤借用 default profile 的環境變數。較舊 Hermes 沒有該 helper
    時才退回一般環境變數。
    """

    try:
        from gateway.platforms._shared import get_scoped_secret
    except ImportError:
        return os.getenv(name, "").strip()
    try:
        return str(get_scoped_secret(name) or "").strip()
    except Exception:
        return ""


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        configured = os.getenv("HERMES_HOME", "").strip()
        if configured:
            return Path(configured)
        return Path.home() / ".hermes"


def shared_state_path() -> Path:
    """回傳 Controller 與本機各 profile 都能開啟的共用 ledger 路徑。

    Hermes 的 named profile 有自己的 ``HERMES_HOME``；若直接使用它，Controller
    與 OT 會各寫一份 SQLite。新版 Hermes 提供 machine-root helper，舊版則以
    Bot Mode 的既有 helper／profile 目錄形狀保守回退。
    """

    configured = os.getenv(STATE_PATH_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    try:
        from hermes_constants import get_default_hermes_root

        root = Path(get_default_hermes_root())
    except Exception:
        home = _hermes_home()
        try:
            from tools.bot_mode_probe import _hermes_root

            root = Path(_hermes_root(home))
        except Exception:
            root = home.parent.parent if home.parent.name == "profiles" else home
    return root / "plugin-data" / PLUGIN_STATE_DIRECTORY / "bridge.sqlite3"


def _string_list(value: Any, *, field: str) -> tuple[str, ...]:
    if isinstance(value, str):
        candidates: Iterable[Any] = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        candidates = value
    else:
        raise BridgeConfigurationError(f"{field} 必須是 Telegram User ID 清單。")
    values = tuple(str(item).strip() for item in candidates if str(item).strip())
    if not values:
        raise BridgeConfigurationError(f"{field} 不可為空；外掛預設拒絕所有使用者。")
    if any(not value.lstrip("-").isdigit() for value in values):
        raise BridgeConfigurationError(f"{field} 只能包含數字 Telegram User ID。")
    return values


def _positive_number(extra: dict[str, Any], key: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = extra.get(key, default)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise BridgeConfigurationError(f"{key} 必須是數字。") from exc
    if not minimum <= value <= maximum:
        raise BridgeConfigurationError(f"{key} 必須介於 {minimum:g} 與 {maximum:g}。")
    return value


@dataclass(frozen=True)
class BridgeConfig:
    """不含可記錄秘密的 bridge 執行設定。"""

    bot_token: str
    backend_token: str
    backend_url: str
    controller_profile: str
    allowed_user_ids: tuple[str, ...]
    state_path: Path
    home_chat_id: str | None
    telegram_poll_timeout_seconds: int
    history_poll_interval_seconds: float
    rpc_timeout_seconds: float
    retry_base_seconds: float
    retry_max_seconds: float

    @classmethod
    def from_platform_config(cls, platform_config: Any) -> "BridgeConfig":
        extra = dict(getattr(platform_config, "extra", None) or {})
        bot_token = _scoped_secret(BOT_TOKEN_ENV)
        backend_token = _scoped_secret(BACKEND_TOKEN_ENV)
        if not bot_token:
            raise BridgeConfigurationError(f"缺少秘密設定 {BOT_TOKEN_ENV}。")
        if not backend_token:
            raise BridgeConfigurationError(f"缺少秘密設定 {BACKEND_TOKEN_ENV}。")

        backend_url = str(extra.get("backend_url") or "").strip()
        parsed = urlparse(backend_url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.netloc:
            raise BridgeConfigurationError("backend_url 必須是完整 ws:// 或 wss:// URL。")
        if "token=" in (parsed.query or "").lower():
            raise BridgeConfigurationError(
                "backend_url 不可包含 token；請改用 TELEGRAM_CANONICAL_BRIDGE_BACKEND_TOKEN。"
            )

        controller_profile = str(extra.get("controller_profile") or "default").strip()
        if not controller_profile:
            raise BridgeConfigurationError("controller_profile 不可為空。")
        if any(char.isspace() for char in controller_profile):
            raise BridgeConfigurationError("controller_profile 不可包含空白。")

        allowed_user_ids = _string_list(extra.get("allowed_user_ids"), field="allowed_user_ids")
        raw_home_chat_id = extra.get("home_chat_id")
        home_chat_id = str(raw_home_chat_id).strip() if raw_home_chat_id is not None else None
        if home_chat_id == "":
            home_chat_id = None
        if home_chat_id and not home_chat_id.lstrip("-").isdigit():
            raise BridgeConfigurationError("home_chat_id 必須是數字 Telegram chat ID。")

        configured_state_path = extra.get("state_path") or os.getenv(STATE_PATH_ENV, "").strip()
        if configured_state_path:
            state_path = Path(str(configured_state_path)).expanduser()
        else:
            state_path = shared_state_path()

        poll_timeout = int(_positive_number(
            extra, "telegram_poll_timeout_seconds", 40, minimum=1, maximum=50
        ))
        history_poll = _positive_number(
            extra, "history_poll_interval_seconds", 3, minimum=0.5, maximum=60
        )
        rpc_timeout = _positive_number(extra, "rpc_timeout_seconds", 25, minimum=3, maximum=180)
        retry_base = _positive_number(extra, "retry_base_seconds", 2, minimum=1, maximum=60)
        retry_max = _positive_number(extra, "retry_max_seconds", 60, minimum=retry_base, maximum=3600)

        return cls(
            bot_token=bot_token,
            backend_token=backend_token,
            backend_url=backend_url,
            controller_profile=controller_profile,
            allowed_user_ids=allowed_user_ids,
            state_path=state_path,
            home_chat_id=home_chat_id,
            telegram_poll_timeout_seconds=poll_timeout,
            history_poll_interval_seconds=history_poll,
            rpc_timeout_seconds=rpc_timeout,
            retry_base_seconds=retry_base,
            retry_max_seconds=retry_max,
        )

    def is_allowed_user(self, user_id: str | int | None) -> bool:
        return user_id is not None and str(user_id) in self.allowed_user_ids


def check_secrets_present() -> bool:
    """供 Hermes 狀態頁呼叫的無副作用設定探針。"""

    return bool(_scoped_secret(BOT_TOKEN_ENV) and _scoped_secret(BACKEND_TOKEN_ENV))
