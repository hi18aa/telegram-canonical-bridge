"""Hermes Agent Task Bridge 的一般 tools/hooks plugin 入口。"""

from __future__ import annotations

from typing import Any


def register(ctx: Any) -> None:
    from .tools import register_tools

    register_tools(ctx)


__all__ = ["register"]
