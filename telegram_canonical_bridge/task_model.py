"""可追蹤 ``message_agent`` 任務的領域模型與 Telegram 呈現。

這裡只保存可安全顯示的狀態摘要，以及追蹤任務的清理後 OT 最終答覆摘要；
原始 prompt、conversation history、工具參數與工具結果不會寫入任務時間線，
避免把 Controller／OT 的私密內容意外轉送到 Telegram。
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone


TASK_ID_RE = re.compile(r"^TCB-[0-9]{8}-[A-F0-9]{6}$", re.IGNORECASE)
TASK_MARKER_RE = re.compile(r"\[TCB-TASK:(TCB-[0-9]{8}-[A-F0-9]{6})\]", re.IGNORECASE)

TERMINAL_TASK_STATUSES = frozenset({"completed", "finished", "failed", "cancelled"})

TASK_STATUS_LABELS = {
    "dispatching": "準備派工",
    "dispatched": "已排入 Hermes 背景程序",
    "running": "OT 處理中",
    "waiting": "等待留言／外部條件",
    "blocked": "需要協助",
    "returning": "OT 已產生回覆",
    "completed": "已完成",
    "finished": "背景程序已結束（結果待確認）",
    "failed": "失敗",
    "cancelled": "已取消",
}


@dataclass(frozen=True)
class TaskRecord:
    id: str
    chat_id: str
    origin_profile: str
    origin_session_id: str
    origin_turn_id: str
    origin_tool_call_id: str
    parent_task_id: str | None
    target: str
    process_id: str | None
    worker_profile: str | None
    worker_session_id: str | None
    worker_turn_id: str | None
    status: str
    progress: str
    evidence: str
    exit_code: int | None
    last_error: str
    telegram_message_id: str | None
    pending_notes: int
    created_at: float
    updated_at: float
    finished_at: float | None
    revision: int

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_TASK_STATUSES


@dataclass(frozen=True)
class TaskNote:
    id: int
    task_id: str
    text: str
    created_at: float


def new_task_id(now: datetime | None = None) -> str:
    instant = now or datetime.now(timezone.utc)
    return f"TCB-{instant.strftime('%Y%m%d')}-{secrets.token_hex(3).upper()}"


def normalize_task_id(value: object) -> str:
    candidate = str(value or "").strip().upper()
    return candidate if TASK_ID_RE.fullmatch(candidate) else ""


def task_marker(task_id: str) -> str:
    normalized = normalize_task_id(task_id)
    if not normalized:
        raise ValueError("無效的 bridge task ID。")
    return f"[TCB-TASK:{normalized}]"


def extract_task_id(text: object) -> str:
    match = TASK_MARKER_RE.search(str(text or ""))
    return normalize_task_id(match.group(1)) if match else ""


def sanitize_progress(value: object, *, limit: int = 500) -> str:
    """把可公開狀態壓成單段文字，並移除可偽造的 task marker。"""

    text = TASK_MARKER_RE.sub("[task]", str(value or ""))
    text = " ".join(text.replace("\x00", "").split())
    return text[:limit]


def _local_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def render_task_card(task: TaskRecord) -> str:
    status = TASK_STATUS_LABELS.get(task.status, task.status)
    lines = [
        f"🧭 Hermes 任務 {task.id}",
        f"對象：@{task.target.lstrip('@')}",
        f"狀態：{status}",
        f"進度：{task.progress or '尚無進一步證據'}",
    ]
    if task.pending_notes:
        lines.append(f"留言：{task.pending_notes} 則待 OT 讀取")
    if task.process_id:
        lines.append(f"背景 handle：{task.process_id}")
    if task.exit_code is not None:
        lines.append(f"程序 exit code：{task.exit_code}")
    if task.last_error:
        lines.append(f"錯誤：{sanitize_progress(task.last_error, limit=280)}")
    lines.append(f"證據：{task.evidence or 'bridge ledger'}")
    lines.append(f"更新：{_local_time(task.updated_at)}")
    if not task.terminal:
        lines.append(f"回覆此訊息可留言，或使用 /tell {task.id} <內容>")
    return "\n".join(lines)


def render_task_event(task: TaskRecord) -> str:
    """產生適合不可變 Telegram 時間線的簡潔任務事件。"""

    status = TASK_STATUS_LABELS.get(task.status, task.status)
    emoji = {
        "dispatching": "🧭",
        "dispatched": "📨",
        "running": "🔄",
        "waiting": "⏳",
        "blocked": "⚠️",
        "returning": "📬",
        "completed": "✅",
        "finished": "⚠️",
        "failed": "❌",
        "cancelled": "⛔",
    }.get(task.status, "ℹ️")
    lines = [
        f"{emoji} 任務 {task.id}｜@{task.target.lstrip('@')}",
        f"{status}：{task.progress or '尚無進一步證據'}",
    ]
    if task.last_error and task.status in {"blocked", "failed"}:
        lines.append(f"錯誤：{sanitize_progress(task.last_error, limit=280)}")
    if task.status == "dispatching":
        lines.append("後續進度會以新訊息發布；回覆任一任務訊息都可留言。")
    return "\n".join(lines)


def render_task_list(tasks: list[TaskRecord]) -> str:
    if not tasks:
        return "目前沒有可顯示的 message_agent 任務。"
    lines = ["最近的 message_agent 任務："]
    for task in tasks:
        label = TASK_STATUS_LABELS.get(task.status, task.status)
        lines.append(f"• {task.id}｜@{task.target.lstrip('@')}｜{label}")
    lines.append("使用 /task <task_id> 查看證據與進度。")
    return "\n".join(lines)
