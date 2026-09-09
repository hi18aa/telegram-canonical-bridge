"""平台外掛的 eager tools/hooks 入口。"""

from __future__ import annotations

from typing import Any

from .telegram_canonical_bridge.agent_tasks import register_agent_tasks
from .telegram_canonical_bridge.task_features import register_task_features


def register_tools(ctx: Any) -> None:
    register_task_features(ctx)
    register_agent_tasks(ctx)
