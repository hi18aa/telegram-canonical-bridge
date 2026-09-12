"""Hermes hooks 與專門 Bot 可呼叫的任務狀態工具。

外掛只觀察自己的 ``agent_task_*`` 工作流程，不包裝或覆寫內建
``message_agent``。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent_tasks import AGENT_TASK_TOOL_NAMES, prepare_start
from .config import shared_state_path
from .native_delivery import kick_native_outbox
from .state import BridgeState
from .task_completion import completion_reason, settle_task_completion
from .task_model import extract_task_id, sanitize_progress


logger = logging.getLogger(__name__)

TOOLSET_NAME = "telegram_canonical_bridge"
BRIDGE_TOOL_NAMES = frozenset({
    "bridge_task_update",
    "bridge_task_inbox",
    "bridge_task_status",
}) | AGENT_TASK_TOOL_NAMES
_STATE_CACHE: dict[Path, BridgeState] = {}
_STATE_CACHE_LOCK = threading.Lock()


def _transition_task(state: BridgeState, task_id: str, **changes: Any):
    """更新 ledger 後，順手嘗試送出原生平台時間線；失敗仍留在 outbox。"""

    updated = state.transition_task(task_id, **changes)
    kick_native_outbox(state)
    return updated


@dataclass(frozen=True)
class _ProcessCompletion:
    process_id: str
    exit_code: int | None
    output: str
    reason: str


_CURRENT_COMPLETION_RE = re.compile(
    r"^\s*\[IMPORTANT:\s*Background process\s+"
    r"(?P<process_id>[A-Za-z0-9_.:-]+)\s+.*?"
    r"\(exit code\s+(?P<exit_code>-?\d+|\?)(?:,[^)]+)?\)\.",
    re.IGNORECASE | re.DOTALL,
)
def _parse_process_completion(value: object) -> _ProcessCompletion | None:
    """讀取 Hermes 公開的背景完成通知；格式不符時完全忽略。

    解析器只用 opaque process ID 做 ledger 關聯，不讀 Command 欄位。
    """

    text = str(value or "")
    match = _CURRENT_COMPLETION_RE.match(text)
    if match is None:
        return None
    raw_exit = match.group("exit_code")
    exit_code = int(raw_exit) if raw_exit != "?" else None
    output = ""
    delimiter = "\nOutput:\n"
    position = text.find(delimiter, match.end())
    if position >= 0:
        output = text[position + len(delimiter):].rstrip()
        if output.endswith("]"):
            output = output[:-1].rstrip()
    return _ProcessCompletion(
        process_id=match.group("process_id"),
        exit_code=exit_code,
        output=output,
        reason=completion_reason(output),
    )


def _observe_process_completion(state: BridgeState, value: object) -> bool:
    completion = _parse_process_completion(value)
    if completion is None:
        return False
    task = state.task_by_process_id(completion.process_id)
    if task is None:
        return True
    settle_task_completion(
        state,
        task.id,
        exit_code=completion.exit_code,
        output=completion.output,
        reason=completion.reason,
        origin="Hermes completion notification",
    )
    kick_native_outbox(state)
    return True

def _state() -> BridgeState:
    path = shared_state_path().resolve()
    with _STATE_CACHE_LOCK:
        state = _STATE_CACHE.get(path)
        if state is None:
            state = _STATE_CACHE[path] = BridgeState(path)
        return state


def _current_profile() -> str:
    try:
        from hermes_constants import get_hermes_home, named_profile_home

        home = Path(get_hermes_home())
        named = named_profile_home(home)
        return named.name if named is not None else "default"
    except Exception:
        return "default"


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def _tool_activity(tool_name: str) -> tuple[str, str] | None:
    if tool_name.startswith("browser_"):
        return "Bot 正在操作瀏覽器；bridge 只確認到 browser tool 呼叫，不推測畫面結果。", "hook:browser"
    if tool_name == "computer_use":
        return "Bot 正在操作電腦介面；bridge 已觀察到 computer_use 呼叫。", "hook:computer_use"
    if tool_name in {"terminal", "process_manage"}:
        return "Bot 正在執行或檢查本機程序。", "hook:terminal"
    if tool_name in {"write_file", "patch"}:
        return "Bot 正在修改檔案。", "hook:file-write"
    if tool_name in {"web_search", "web_extract"}:
        return "Bot 正在查詢外部資料。", "hook:web"
    return None


def _before_tool(
    tool_name: str,
    args: dict[str, Any],
    session_id: str = "",
    tool_call_id: str = "",
    turn_id: str = "",
    **_: Any,
) -> dict[str, Any] | None:
    """建立派工 intent，或記錄已綁定 worker 的高階工具活動。"""

    try:
        state = _state()
        if tool_name == "agent_task_start":
            return prepare_start(
                state,
                args or {},
                origin_profile=_current_profile(),
                session_id=str(session_id or ""),
                turn_id=str(turn_id or ""),
                tool_call_id=str(tool_call_id or ""),
            )
        if tool_name in BRIDGE_TOOL_NAMES:
            return None
        task = state.task_for_worker(session_id, turn_id)
        activity = _tool_activity(tool_name)
        if (
            task is not None
            and not task.terminal
            and activity is not None
            and not state.latest_explicit_progress(task.id)
        ):
            # Bot 的明確、使用者可見里程碑比後續泛用 tool category 更有資訊。
            _transition_task(
                state,
                status="running",
                task_id=task.id,
                progress=activity[0],
                evidence=activity[1],
            )
        return None
    except Exception:
        logger.warning("Agent Task Bridge pre_tool_call failed open", exc_info=True)
        return None


def _after_tool(
    tool_name: str,
    result: Any,
    session_id: str = "",
    tool_call_id: str = "",
    turn_id: str = "",
    status: str = "",
    error_message: str | None = None,
    **_: Any,
) -> None:
    try:
        state = _state()
        if tool_name in BRIDGE_TOOL_NAMES:
            return
        task = state.task_for_worker(session_id, turn_id)
        if (
            task is not None
            and not task.terminal
            and status in {"error", "blocked"}
            and not state.latest_explicit_progress(task.id)
        ):
            _transition_task(
                state,
                task.id,
                status="running",
                progress=f"Bot 的 {tool_name} 呼叫未成功；Bot 仍可調整後繼續。",
                evidence=f"hook:{status}",
            )
    except Exception:
        logger.warning("Agent Task Bridge post_tool_call observer failed", exc_info=True)


def _before_llm(
    session_id: str,
    user_message: str,
    turn_id: str = "",
    **_: Any,
) -> dict[str, str] | None:
    try:
        state = _state()
        # 背景 terminal 的完成通知會以合成 user turn 回到派工者。先依 process
        # ID 收斂狀態，且不要把 output 內的 task marker 誤綁成 Controller 自己
        # 的 worker turn。
        if _observe_process_completion(state, user_message):
            return None
        task_id = extract_task_id(user_message)
        if not task_id:
            # Controller 指引已由 register_system_prompt_section 提供。這裡若再回傳
            # dynamic context，Hermes 會把它接在當次 user message 後方；Controller
            # 便可能將那段控制文字誤收進 exact_payload。普通 turn 不再注入文字。
            return None
        task = state.task(task_id)
        if task is None:
            return None
        worker_profile = _current_profile()
        if task.target != worker_profile:
            # marker 不是授權；只有任務指定的 profile 可以接手這個 worker turn。
            return None
        bound = state.bind_worker(
            task_id,
            worker_profile=worker_profile,
            worker_session_id=str(session_id or ""),
            worker_turn_id=str(turn_id or ""),
        )
        if bound is None:
            return None
        kick_native_outbox(state)
        # 只做身分綁定與狀態觀測，不再把逐 task 控制文字注入模型 context。
        # 控制面位於 runner handoff 的前段；逐字資料面則只存在獨立 artifact，
        # 因此不會出現「正文結尾緊接 bridge_task_* 指引」的連續內容。
        return None
    except Exception:
        logger.warning("Agent Task Bridge pre_llm_call failed open", exc_info=True)
        return None


def _after_llm(
    session_id: str,
    turn_id: str = "",
    assistant_response: str = "",
    **_: Any,
) -> None:
    try:
        state = _state()
        state.complete_worker_turn(
            session_id,
            turn_id,
            assistant_response=assistant_response,
        )
        kick_native_outbox(state)
    except Exception:
        logger.warning("Agent Task Bridge post_llm_call observer failed", exc_info=True)


def _on_session_end(
    session_id: str,
    turn_id: str = "",
    failed: bool = False,
    interrupted: bool = False,
    turn_exit_reason: str = "",
    **_: Any,
) -> None:
    if not failed and not interrupted:
        return
    try:
        state = _state()
        task = state.task_for_worker(session_id, turn_id)
        if task is None or task.terminal or task.status == "returning":
            return
        reason = sanitize_progress(turn_exit_reason, limit=280)
        kind = "failed" if failed else "interrupted"
        _transition_task(
            state,
            task.id,
            status="settling",
            progress=(
                "Bot turn 在 final 前中斷；bridge 正等待原 runner 的結束證據。"
                "此時不會重送原任務，也不能把網站或其他外部結果判定為失敗。"
            ),
            evidence=f"hook:on_session_end {kind}",
            last_error=reason or f"worker turn {kind}",
        )
    except Exception:
        logger.warning("Agent Task Bridge session-end observer failed", exc_info=True)


def _authorized_worker_task(
    state: BridgeState, task_id: str, session_id: str
) -> tuple[Any, str | None]:
    task = state.task(task_id)
    if task is None:
        return None, "找不到 task。"
    bound = state.task_for_worker(str(session_id or ""))
    if bound is None or bound.id != task.id:
        return None, "這個 Hermes session 不是該 task 的已綁定 Bot turn。"
    return task, None


def bridge_task_update(args: dict[str, Any], **kwargs: Any) -> str:
    state = _state()
    task_id = str(args.get("task_id") or "")
    task, error = _authorized_worker_task(state, task_id, str(kwargs.get("session_id") or ""))
    if error:
        return _json({"ok": False, "error": error})
    requested = str(args.get("status") or "working").strip().lower()
    mapped = {
        "working": "running",
        "waiting": "waiting",
        "blocked": "blocked",
        "result": "running",
    }.get(requested)
    message = sanitize_progress(args.get("message"))
    if mapped is None or not message:
        return _json({"ok": False, "error": "status 或 message 無效。"})
    updated = _transition_task(
        state,
        task.id,
        status=mapped,
        progress=message,
        evidence=(
            "Bot explicit bridge_task_result"
            if requested == "result"
            else "Bot explicit bridge_task_update"
        ),
    )
    return _json({
        "ok": updated is not None,
        "task_id": task.id,
        "status": updated.status if updated else task.status,
        "message": "任務進度已排入原生訊息平台時間線。",
    })


def bridge_task_inbox(args: dict[str, Any], **kwargs: Any) -> str:
    state = _state()
    task_id = str(args.get("task_id") or "")
    task, error = _authorized_worker_task(state, task_id, str(kwargs.get("session_id") or ""))
    if error:
        return _json({"ok": False, "error": error})
    acknowledge = bool(args.get("acknowledge", True))
    updated, notes = state.read_task_notes(task.id, mark_read=acknowledge)
    kick_native_outbox(state)
    return _json({
        "ok": True,
        "task_id": task.id,
        "acknowledged": acknowledge and bool(notes),
        "messages": [{"id": note.id, "text": note.text} for note in notes],
        "remaining_unread": updated.pending_notes if updated else 0,
    })


def bridge_task_status(args: dict[str, Any], **_: Any) -> str:
    state = _state()
    requested = str(args.get("task_id") or "").strip()
    tasks = [state.task(requested)] if requested else state.list_tasks(limit=10)
    rows = [
        {
            "task_id": task.id,
            "target": task.target,
            "status": task.status,
            "lifecycle": task.lifecycle,
            "resumable": task.resumable,
            "final": task.final,
            "progress": task.progress,
            "evidence": task.evidence,
            "process_id": task.process_id,
            "exit_code": task.exit_code,
            "worker_started": task.worker_started,
            "final_observed": task.final_observed,
            "worker_profile": task.worker_profile,
            "pending_notes": task.pending_notes,
            "updated_at": task.updated_at,
        }
        for task in tasks if task is not None
    ]
    return _json({"ok": bool(rows), "tasks": rows})


def register_task_features(ctx: Any) -> None:
    """註冊 worker 狀態工具與任務生命週期 hooks。"""

    ctx.register_tool(
        name="bridge_task_update",
        toolset=TOOLSET_NAME,
        schema={
            "name": "bridge_task_update",
            "description": (
                "Update a tracked Agent Task only when a meaningful, "
                "verifiable milestone changed. Never report private reasoning."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "TCB task ID from injected context."},
                    "status": {
                        "type": "string",
                        "enum": ["working", "waiting", "blocked", "result"],
                        "description": "Evidence-based current state.",
                    },
                    "message": {
                        "type": "string",
                        "description": "Concise user-facing progress, max 500 characters.",
                    },
                },
                "required": ["task_id", "status", "message"],
            },
        },
        handler=bridge_task_update,
        description="回報可驗證的 Bot 任務進度",
        emoji="🧭",
    )
    ctx.register_tool(
        name="bridge_task_inbox",
        toolset=TOOLSET_NAME,
        schema={
            "name": "bridge_task_inbox",
            "description": (
                "Read follow-up messages attached by the controller to this tracked task. "
                "Check at natural milestones and before the final response."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "acknowledge": {"type": "boolean", "default": True},
                },
                "required": ["task_id"],
            },
        },
        handler=bridge_task_inbox,
        description="讀取主 Agent 的任務留言",
        emoji="📥",
    )
    ctx.register_tool(
        name="bridge_task_status",
        toolset=TOOLSET_NAME,
        schema={
            "name": "bridge_task_status",
            "description": "Read evidence-backed status for one or recent Agent Task Bridge tasks.",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": [],
            },
        },
        handler=bridge_task_status,
        description="查詢 Agent Task Bridge ledger",
        emoji="📋",
    )
    ctx.register_hook("pre_tool_call", _before_tool)
    ctx.register_hook("post_tool_call", _after_tool)
    ctx.register_hook("pre_llm_call", _before_llm)
    ctx.register_hook("post_llm_call", _after_llm)
    ctx.register_hook("on_session_end", _on_session_end)
