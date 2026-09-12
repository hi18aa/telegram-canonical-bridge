"""Agent Task Bridge 的領域模型與公開時間線文字。"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone


TASK_ID_RE = re.compile(r"^TCB-[0-9]{8}-[A-F0-9]{6}$", re.IGNORECASE)
TASK_MARKER_RE = re.compile(r"\[TCB-TASK:(TCB-[0-9]{8}-[A-F0-9]{6})\]", re.IGNORECASE)

FINAL_TASK_STATUSES = frozenset({"completed", "failed", "cancelled"})
RECOVERABLE_TERMINAL_TASK_STATUSES = frozenset({"interrupted", "unconfirmed"})
# ``terminal`` 在 ledger 內表示目前沒有可執行中的 runner。可接續終態仍與
# 真正結案分開，公開契約應優先看 ``lifecycle``／``resumable``。
TERMINAL_TASK_STATUSES = FINAL_TASK_STATUSES | RECOVERABLE_TERMINAL_TASK_STATUSES

TASK_STATUS_LABELS = {
    "dispatching": "準備派工",
    "dispatched": "已排入 Hermes 背景程序",
    "running": "Bot 處理中",
    "waiting": "等待留言／外部條件",
    "blocked": "需要協助",
    "settling": "Bot turn 已中斷，等待 runner 收斂",
    "continuing": "同一 task 正在接續",
    "stopping": "正在取消",
    "returning": "Bot 已產生回覆",
    "completed": "已完成",
    "unconfirmed": "執行結果未確認",
    "interrupted": "可接續（原 Bot turn 中斷）",
    "failed": "失敗",
    "cancelled": "已取消",
}


@dataclass(frozen=True)
class TaskRecord:
    id: str
    delivery_target: str
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
    pending_notes: int
    created_at: float
    updated_at: float
    finished_at: float | None
    revision: int

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_TASK_STATUSES

    @property
    def resumable(self) -> bool:
        return (
            self.status in RECOVERABLE_TERMINAL_TASK_STATUSES
            # v0.6.4 會把已綁定 worker、尚無 final 的 abnormal turn 直接寫成
            # failed。只在有 worker session 證據時把這種既有紀錄視為可接續；
            # 不改 ledger、不自動 spawn。
            or (
                self.status == "failed"
                and self.worker_started
                and not self.final_observed
            )
        )

    @property
    def final(self) -> bool:
        # 舊版 ``failed`` 若其實是已啟動 worker 的無 final 中斷，公開契約不能
        # 同時宣稱 final=true 與 resumable=true。
        return self.status in FINAL_TASK_STATUSES and not self.resumable

    @property
    def lifecycle(self) -> str:
        if self.status == "settling":
            return "settling"
        if self.resumable:
            return "resumable"
        if self.final:
            return "terminated"
        return "active"

    @property
    def worker_started(self) -> bool:
        return bool(self.worker_session_id)

    @property
    def final_observed(self) -> bool:
        evidence = self.evidence.lower()
        return (
            self.status == "returning"
            or "post_llm_call" in evidence
            or "with reply" in evidence
        )


@dataclass(frozen=True)
class TaskNote:
    id: int
    task_id: str
    text: str
    created_at: float


@dataclass(frozen=True)
class OutboxRecord:
    id: int
    task_id: str
    delivery_target: str
    content: str
    attempts: int


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


def render_task_event(task: TaskRecord) -> str:
    """產生不可變、適合由 Hermes 原生平台送出的任務事件。"""

    status = (
        "可接續（舊版將 worker 中斷標為 failed）"
        if task.status == "failed" and task.resumable
        else TASK_STATUS_LABELS.get(task.status, task.status)
    )
    emoji = {
        "dispatching": "🧭",
        "dispatched": "📨",
        "running": "🔄",
        "waiting": "⏳",
        "blocked": "⚠️",
        "settling": "⏳",
        "continuing": "↪️",
        "stopping": "🛑",
        "returning": "📬",
        "completed": "✅",
        "unconfirmed": "⚠️",
        "interrupted": "↪️",
        "failed": "❌",
        "cancelled": "⛔",
    }.get(task.status, "ℹ️")
    if task.status == "failed" and task.resumable:
        emoji = "↪️"
    lines = [
        f"{emoji} 任務 {task.id}｜@{task.target.lstrip('@')}",
        f"{status}：{task.progress or '尚無進一步證據'}",
    ]
    if task.last_error and task.status in {
        "blocked", "settling", "interrupted", "unconfirmed", "failed"
    }:
        lines.append(f"錯誤：{sanitize_progress(task.last_error, limit=280)}")
    if task.status == "dispatching":
        lines.append("後續進度會以新訊息發布；可用 /agenttask message 補充指示。")
    elif task.status == "settling":
        lines.append("請等 runner 完成事件；此時不要另開或重送相同任務。")
    elif task.resumable:
        lines.append(
            "可用 agent_task_message 補入查詢／修復／reconcile 指示，接續同一 task；"
            "bridge 不會自動重播原任務。"
        )
    return "\n".join(lines)
