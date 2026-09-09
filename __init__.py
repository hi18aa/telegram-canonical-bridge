"""Hermes 平台外掛入口。

保持 import-light，讓 Hermes 在 CLI／OT profile 只需載入任務工具與 hooks，
不必初始化 Telegram adapter。
"""

from __future__ import annotations

from typing import Any


def register(ctx: Any) -> None:
    from .tools import register_tools

    register_tools(ctx)
    from .adapter import register as register_platform

    register_platform(ctx)


__all__ = ["register"]
